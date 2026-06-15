"""ffmpeg/ffprobe plumbing for Mutator. No Gradio, no host imports.

Locates the ffmpeg/ffprobe binaries (honoring the host's setup as well as a
plugin-specific ``MUTATOR_FFMPEG`` override), runs them with a friendly error
surface (``FFmpegError`` carrying the tail of stderr), and probes a clip into a
single canonical metadata dict the rest of the plugin relies on.

``probe`` prefers ffprobe for an EXACT frame rate (``r_frame_rate`` parsed as a
rational so 23.976 never collapses to 24) and reliable audio-stream detection;
when ffprobe is unavailable it falls back to the host's ``get_video_info`` and
flags the result ``_fps_inexact`` so the UI can warn about coarse frame math.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional


class FFmpegError(Exception):
    """Raised when an ffmpeg/ffprobe invocation exits non-zero or times out."""


# --------------------------------------------------------------------------- #
#  binaries                                                                    #
# --------------------------------------------------------------------------- #

def ffmpeg_path() -> Optional[str]:
    """Resolve the ffmpeg binary: ``MUTATOR_FFMPEG`` env override, then PATH, then
    the bundled imageio-ffmpeg build. ``None`` if nothing usable is found."""
    cand = os.environ.get("MUTATOR_FFMPEG") or shutil.which("ffmpeg")
    if cand and Path(cand).exists():
        return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffprobe_path() -> Optional[str]:
    """Resolve ffprobe from PATH (``None`` if absent — ``probe`` then falls back)."""
    return shutil.which("ffprobe") or None


# --------------------------------------------------------------------------- #
#  run                                                                         #
# --------------------------------------------------------------------------- #

def run(args: list[str], timeout: int = 3600) -> str:
    """Run an ffmpeg/ffprobe command and return its stdout.

    ``args[0]`` must already be the binary the caller obtained from
    ``ffmpeg_path()`` / ``ffprobe_path()``. On a non-zero exit (or timeout) raise
    ``FFmpegError`` carrying the last 24 lines of stderr so callers can surface a
    compact, actionable message instead of a wall of ffmpeg noise.
    """
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s.")
    except FileNotFoundError:
        raise FFmpegError(f"Binary not found: {args[0] if args else '?'}")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-24:])
        raise FFmpegError(f"ffmpeg exited {proc.returncode}:\n{tail}")
    return proc.stdout or ""


# --------------------------------------------------------------------------- #
#  probe                                                                       #
# --------------------------------------------------------------------------- #

def _parse_fps(r_frame_rate) -> float:
    """Parse ffprobe's rational ``r_frame_rate`` (e.g. ``"24000/1001"``) into an
    exact float. Returns 0.0 when unparseable — this is why we prefer ffprobe over
    cv2's ``round(fps)``: 23.976 must not collapse to 24."""
    try:
        num, den = str(r_frame_rate).split("/")
        den = int(den)
        return (int(num) / den) if den else 0.0
    except Exception:
        try:
            return float(r_frame_rate)
        except Exception:
            return 0.0


def probe(path: str, get_video_info: Optional[Callable] = None) -> dict:
    """Probe *path* into a canonical metadata dict.

    Returns ``{"fps", "num_frames", "duration", "width", "height", "has_audio",
    "_fps_inexact"}``. Prefers ffprobe for an exact frame rate (rational
    ``r_frame_rate``), a reliable frame count (``nb_frames`` with a
    ``duration * fps`` fallback) and audio-stream presence. When ffprobe is
    unavailable (or fails) and *get_video_info* is callable, fall back to the
    host's ``get_video_info(path) -> (fps, width, height, num_frames)`` and set
    ``_fps_inexact=True`` so the UI can warn about coarse frame math.

    Raises ``FFmpegError`` only when neither path can yield video metadata.
    """
    exe = ffprobe_path()
    if exe:
        try:
            return _probe_ffprobe(exe, path)
        except Exception:
            pass  # fall through to the host helper / hard failure

    if callable(get_video_info):
        try:
            fps, width, height, num_frames = get_video_info(path)
            fps = float(fps or 0.0)
            num_frames = int(num_frames or 0)
            duration = (num_frames / fps) if fps > 0 else 0.0
            return {
                "fps": fps,
                "num_frames": num_frames,
                "duration": duration,
                "width": int(width or 0),
                "height": int(height or 0),
                "has_audio": False,        # host helper does not report audio
                "_fps_inexact": True,
            }
        except Exception as exc:
            raise FFmpegError(f"Could not read video info for {path}: {exc}")

    raise FFmpegError(
        f"Could not probe {path}: ffprobe not found and no get_video_info fallback."
    )


def _probe_ffprobe(exe: str, path: str) -> dict:
    """Exact probe via ffprobe JSON. Raises if there is no video stream."""
    out = run([exe, "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], timeout=60)
    data = json.loads(out)

    streams = data.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    if vs is None:
        raise ValueError(f"No video stream found in {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    fps = _parse_fps(vs.get("r_frame_rate") or vs.get("avg_frame_rate") or "0/1")
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    if duration <= 0:
        duration = float(vs.get("duration", 0) or 0)

    # nb_frames is unreliable for some codecs; fall back to duration * fps.
    nb = vs.get("nb_frames")
    if nb is not None and str(nb).isdigit():
        num_frames = int(nb)
    else:
        num_frames = int(round(duration * fps)) if fps > 0 else 0

    return {
        "fps": fps,
        "num_frames": num_frames,
        "duration": duration,
        "width": int(vs.get("width", 0) or 0),
        "height": int(vs.get("height", 0) or 0),
        "has_audio": has_audio,
        "_fps_inexact": False,
    }
