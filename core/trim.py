"""Frame-accurate trim & split engine for the Mutator plugin.

Ported essentially verbatim from Trimline-Wan2GP's ``core/trim.py``
(``trim_video_precise`` + the ``frame``/``sec`` boundary helpers), with a new
``split_at_frame`` built on top as two ``trim_video_precise`` calls.

Pure ffmpeg; no Gradio or host imports, so it stays unit-testable on its own.
All ffmpeg invocations are routed through :mod:`core.ffmpeg` (the plugin's single
ffmpeg gateway) so binary discovery and error reporting are consistent across the
suite. Audio probing also goes through that gateway, falling back gracefully when
ffprobe is unavailable.
"""
from __future__ import annotations

import json
import subprocess


# --- pure helpers (unit-tested, no I/O) -------------------------------------

def frame_to_sec(frame: int, fps: float) -> float:
    """Start of frame *N* on the timeline."""
    return (frame / fps) if fps > 0 else 0.0


def end_frame_to_sec(end_frame: int, fps: float) -> float:
    """End boundary for an *inclusive* end frame: the cut runs up to the start of
    the next frame, so ``end_frame`` itself is kept (one extra frame of time)."""
    return ((end_frame + 1) / fps) if fps > 0 else 0.0


# --- internal: ffmpeg/ffprobe access ----------------------------------------

def _ffmpeg() -> str:
    """Resolve the ffmpeg binary via :mod:`core.ffmpeg` (imported lazily so this
    module still imports cleanly even before the gateway is available)."""
    from . import ffmpeg as _ff

    exe = _ff.ffmpeg_path()
    if not exe:
        raise _ff.FFmpegError("ffmpeg binary not found")
    return exe


def _has_audio(path: str) -> bool:
    """True if *path* has at least one audio stream (so the trim can preserve it).
    Uses :mod:`core.ffmpeg`'s ffprobe; returns ``False`` on any failure."""
    try:
        from . import ffmpeg as _ff

        ffprobe = _ff.ffprobe_path()
        if not ffprobe:
            return False
        cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
               "-show_streams", "-select_streams", "a", str(path)]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=15).stdout
        data = json.loads(out)
        return bool(data.get("streams"))
    except Exception:
        return False


# --- ffmpeg-backed operations -----------------------------------------------

def trim_video_precise(src: str, dst: str,
                       start_sec: float, end_sec: float) -> None:
    """Frame-accurate trim via re-encode (ported from Trimline's
    ``trim_video_precise``: ``-i`` then output-side ``-ss``). We use
    ``-ss start -t duration`` (rather than ``-to``) because, as an *output* option
    after ``-i``, ffmpeg decodes from the start and the duration form is
    unambiguous — giving an exact frame boundary. Audio is preserved (AAC 192k)
    when present; ``-pix_fmt yuv420p`` keeps the result broadly playable.

    Raises :class:`core.ffmpeg.FFmpegError` on failure.
    """
    duration = max(end_sec - start_sec, 0.0)
    cmd = [
        _ffmpeg(), "-y",
        "-i", str(src),
        "-ss", f"{start_sec:.6f}",
        "-t", f"{duration:.6f}",
        # libx264 + yuv420p (4:2:0) require even dimensions; round each down to
        # the nearest even pixel so odd-sized sources (some GIFs / arbitrary
        # uploads) don't crash the encoder. No-op for already-even videos.
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-crf", "16", "-preset", "slow",
        "-pix_fmt", "yuv420p",
    ]
    if _has_audio(src):
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [str(dst)]

    from . import ffmpeg as _ff

    # Generous hard cap: a genuine stall is caught, but a long/high-res clip on
    # preset slow can legitimately take minutes, so the cap is not duration-scaled.
    _ff.run(cmd, timeout=900)


def split_at_frame(src: str, head_dst: str, tail_dst: str,
                   frame: int, fps: float) -> None:
    """Split *src* at *frame* into two clips, frame-accurately.

    ``head`` keeps frames ``[0 .. frame]`` (inclusive) and ``tail`` keeps
    ``[frame + 1 .. end]``. Implemented as two :func:`trim_video_precise` calls:

    - head: ``0`` .. ``end_frame_to_sec(frame, fps)``  (so ``frame`` is kept)
    - tail: ``frame_to_sec(frame + 1, fps)`` .. end of clip

    The tail's end boundary is taken from :func:`core.ffmpeg.probe`'s duration so
    the second segment runs to the true end of the source.

    Raises :class:`core.ffmpeg.FFmpegError` on failure.
    """
    from . import ffmpeg as _ff

    info = _ff.probe(src)
    duration = float(info.get("duration") or 0.0)

    head_end = end_frame_to_sec(frame, fps)
    tail_start = frame_to_sec(frame + 1, fps)

    trim_video_precise(src, head_dst, 0.0, head_end)
    trim_video_precise(src, tail_dst, tail_start, duration)
