"""Mini-timeline filmstrip rendering for the Mutator clip editor.

Builds a single wide, horizontal strip PNG of evenly-spaced frames so the tab's
mini-timeline shows the clip's content at a glance. The strip is regenerated on
every load/edit (each edit writes a new working file), so it always reflects the
current working clip.

Two strategies are provided, tried in order:

1. A one-shot ffmpeg pass (``fps,scale,tile=Nx1``) — fast and dependency-free
   beyond the ffmpeg binary that ``core.ffmpeg`` already resolves.
2. A PIL fallback that pulls ``num_frames`` frames through the host's
   ``get_video_frame`` callback and tiles them left-to-right.

The public entry point never raises: any failure returns ``None`` and the caller
simply renders an empty mini-timeline.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from ..core import ffmpeg, paths


def build_filmstrip(src, num_frames: int = 12, height: int = 72,
                    get_video_frame=None) -> str | None:
    """Render a wide horizontal filmstrip PNG for *src* and return its path.

    *num_frames* evenly-spaced frames are scaled to *height* px tall and tiled
    left-to-right into one PNG written under :func:`core.paths.cache_dir`.

    Tries the ffmpeg ``fps,scale,tile`` pass first; if that produces nothing and
    a host ``get_video_frame(path, idx, return_PIL=True)`` callable is supplied,
    falls back to a PIL tiling of sampled frames. Returns the strip path, or
    ``None`` on any failure (this function never raises).
    """
    if not src:
        return None
    try:
        n = max(1, int(num_frames))
        h = max(8, int(height))
    except (TypeError, ValueError):
        return None

    try:
        src_path = str(src)
        if not Path(src_path).exists():
            return None
    except Exception:
        return None

    dest = str(Path(paths.cache_dir()) / f"strip_{uuid4().hex}.png")

    strip = _ffmpeg_strip(src_path, dest, n, h)
    if strip:
        return strip

    if callable(get_video_frame):
        return _pil_strip(src_path, dest, n, h, get_video_frame)

    return None


# --------------------------------------------------------------------------- #
#  ffmpeg strategy                                                             #
# --------------------------------------------------------------------------- #

def _ffmpeg_strip(src: str, dest: str, n: int, h: int) -> str | None:
    """One-shot ffmpeg pass: sample ~*n* frames, scale to *h* tall, tile Nx1."""
    exe = ffmpeg.ffmpeg_path()
    if not exe:
        return None

    info = {}
    try:
        info = ffmpeg.probe(src)
    except Exception:
        info = {}

    duration = 0.0
    try:
        duration = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    # Sample rate that yields about n frames across the whole clip. With an
    # unknown/zero duration, fall back to a modest fixed rate and cap the tile
    # count to whatever ffmpeg emits.
    if duration > 0:
        rate = max(0.1, n / duration)
    else:
        rate = 1.0

    vf = f"fps={rate:.6f},scale=-1:{h}:flags=bilinear,tile={n}x1"
    try:
        ffmpeg.run(
            [exe, "-y", "-i", src, "-frames:v", "1", "-vf", vf,
             "-an", "-sn", dest],
            timeout=120,
        )
    except Exception:
        return None

    return dest if Path(dest).exists() else None


# --------------------------------------------------------------------------- #
#  PIL fallback strategy                                                       #
# --------------------------------------------------------------------------- #

def _pil_strip(src: str, dest: str, n: int, h: int, get_video_frame) -> str | None:
    """Pull *n* evenly-spaced frames via *get_video_frame* and tile into a PNG."""
    try:
        from PIL import Image
    except Exception:
        return None

    info = {}
    try:
        info = ffmpeg.probe(src)
    except Exception:
        info = {}

    try:
        total = int(info.get("num_frames") or 0)
    except (TypeError, ValueError):
        total = 0

    if total > 0:
        if n == 1:
            indices = [0]
        else:
            indices = [round(i * (total - 1) / (n - 1)) for i in range(n)]
    else:
        # Unknown length: just walk the first n frames; missing ones are skipped.
        indices = list(range(n))

    tiles = []
    for idx in indices:
        try:
            frame = get_video_frame(src, int(idx), return_PIL=True)
        except Exception:
            frame = None
        if frame is None:
            continue
        try:
            img = frame.convert("RGB")
            w = max(1, round(img.width * h / max(1, img.height)))
            tiles.append(img.resize((w, h)))
        except Exception:
            continue

    if not tiles:
        return None

    total_w = sum(t.width for t in tiles)
    try:
        strip = Image.new("RGB", (total_w, h), (0, 0, 0))
        x = 0
        for t in tiles:
            strip.paste(t, (x, 0))
            x += t.width
        strip.save(dest)
    except Exception:
        return None

    return dest if Path(dest).exists() else None
