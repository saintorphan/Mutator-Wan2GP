"""Per-segment rendering, filmstrips and frame extraction for Mutator v0.2.

A :class:`~core.model.Segment` is a non-destructive description of an edit on a
source clip: a trim window (``in_``/``out`` in source seconds) plus independent
crop / resize / flip / speed / reverse / colour. This module turns that
description into pixels in exactly ONE ffmpeg pass, caches the result by a
content signature, and provides the two preview helpers the UI needs — a
horizontally-tiled filmstrip PNG (the timeline clip background) and a single
source-resolution frame (the crop-canvas background).

Nothing here imports Gradio. Filter fragments come from :mod:`core.ops`, the
ffmpeg surface is :mod:`core.ffmpeg`, and cache locations come from
:mod:`core.paths`. Renders are cache-keyed on :func:`segment_render_sig`, which
folds in the source file mtime and every edit, so a trim or any edit is a cache
miss and regenerates; an untouched re-request is an instant cache hit.

Colour units: the model stores UI-anchored values (brightness 50..150, contrast
50..150, saturation 0..200, hue -180..180 deg, warmth -100..100, gamma 0.5..2).
Mapping those to the ffmpeg-native units ``ops.color_vf`` expects is this
module's job (``brightness=(ui-100)/100``, ``contrast=ui/100``,
``saturation=ui/100``; gamma/hue/warmth pass through raw).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import TYPE_CHECKING, Optional

from . import ffmpeg, ops, paths

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from .model import Segment

# Shared encoder options, kept byte-identical to core.ops so renders stack with
# the rest of the plugin. crf 18 is visually lossless for these single passes.
_VENC = ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-preset", "veryfast"]

# Colour-grade neutral anchors in UI units (mirrors model.COLOUR_NEUTRAL). Kept
# locally so render.py never imports the model at runtime.
_COLOUR_NEUTRAL = {
    "brightness": 100,
    "contrast": 100,
    "saturation": 100,
    "hue": 0,
    "warmth": 0,
    "gamma": 1.0,
}


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #

def _source_mtime(path: str) -> float:
    """File mtime of *path* (0.0 if it cannot be stat'd).

    Folded into the render signature so editing the underlying source file (same
    path, new bytes) invalidates every cached render/filmstrip derived from it.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _colour_to_ffmpeg(colour: Optional[dict]) -> dict:
    """Map a UI-unit colour dict to the kwargs ``ops.color_vf`` expects.

    ``brightness=(ui-100)/100`` (-> -0.5..0.5), ``contrast=ui/100`` and
    ``saturation=ui/100`` (-> 0.5..1.5 / 0..2), while ``gamma``/``hue``/``warmth``
    pass through raw. Missing keys fall back to their neutral anchors.
    """
    c = dict(_COLOUR_NEUTRAL)
    if colour:
        c.update(colour)
    return {
        "brightness": (float(c["brightness"]) - 100.0) / 100.0,
        "contrast": float(c["contrast"]) / 100.0,
        "saturation": float(c["saturation"]) / 100.0,
        "gamma": float(c["gamma"]),
        "hue": float(c["hue"]),
        "warmth": float(c["warmth"]),
    }


def _is_neutral_colour(colour: Optional[dict]) -> bool:
    """True when every colour key equals its neutral anchor (so no grade applies)."""
    if not colour:
        return True
    for key, neutral in _COLOUR_NEUTRAL.items():
        if abs(float(colour.get(key, neutral)) - float(neutral)) > 1e-9:
            return False
    return True


def _probe_has_audio(path: str) -> bool:
    """Best-effort audio-stream presence (defaults True so real audio is kept)."""
    try:
        info = ffmpeg.probe(path)
    except Exception:
        return True
    return bool(info.get("has_audio", True))


def _require_ffmpeg() -> str:
    """Resolve the ffmpeg binary or raise ``FFmpegError`` with a clear message."""
    exe = ffmpeg.ffmpeg_path()
    if not exe:
        raise ffmpeg.FFmpegError("ffmpeg binary not found")
    return exe


def _speed_f(seg: "Segment") -> float:
    """The effective playback factor (>0; 1.0 = no change)."""
    s = float(getattr(seg, "speed", 1.0) or 1.0)
    return s if s > 0.01 else 1.0


def _src_len(seg: "Segment") -> float:
    """Trim window length in source seconds (``out - in_``, clamped >= 0)."""
    return max(0.0, float(getattr(seg, "out", 0.0)) - float(getattr(seg, "in_", 0.0)))


# --------------------------------------------------------------------------- #
#  signature                                                                   #
# --------------------------------------------------------------------------- #

def segment_render_sig(seg: "Segment") -> str:
    """Stable hex cache key over everything that affects the rendered output.

    The signature is a SHA-1 of a canonical, ``sort_keys`` JSON of the source
    path, the source file mtime (so editing the bytes invalidates), the trim
    window and every per-clip edit (crop / resize / lock_aspect / flips / speed /
    reverse / colour). Selection, playhead and the render-bookkeeping fields are
    deliberately NOT included — they do not change pixels.
    """
    payload = {
        "source": str(seg.source),
        "mtime": _source_mtime(seg.source),
        "in_": round(float(seg.in_), 6),
        "out": round(float(seg.out), 6),
        "crop": seg.crop,
        "resize": seg.resize,
        "lock_aspect": bool(seg.lock_aspect),
        "flip_h": bool(seg.flip_h),
        "flip_v": bool(seg.flip_v),
        "speed": round(float(seg.speed), 6),
        "reverse": bool(seg.reverse),
        "colour": seg.colour,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
#  render (one ffmpeg pass, cache-first)                                       #
# --------------------------------------------------------------------------- #

def _video_filter_parts(seg: "Segment") -> list[str]:
    """Build the ordered, non-empty ``-vf`` fragments for *seg* (see §3.2).

    Order: crop -> scale -> flip -> colour -> (even-dim safety) -> setpts ->
    reverse. Empty fragments are dropped. The ``reverse`` term is always last in
    the video chain.
    """
    parts: list[str] = []

    has_crop = bool(seg.crop)
    has_resize = bool(seg.resize)

    if has_crop:
        c = seg.crop
        frag = ops.crop_vf(c["x"], c["y"], c["w"], c["h"])
        if frag:
            parts.append(frag)
    if has_resize:
        r = seg.resize
        frag = ops.scale_vf(r.get("w"), r.get("h"))
        if frag:
            parts.append(frag)
    if seg.flip_h or seg.flip_v:
        frag = ops.flip_vf(bool(seg.flip_h), bool(seg.flip_v))
        if frag:
            parts.append(frag)
    if not _is_neutral_colour(seg.colour):
        frag = ops.color_vf(**_colour_to_ffmpeg(seg.colour))
        if frag:
            parts.append(frag)

    # Even-dimension safety only when no explicit crop/scale guarantees it; insert
    # immediately before the speed term so it operates on the post-grade frame.
    if not has_crop and not has_resize:
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    speed = _speed_f(seg)
    if abs(speed - 1.0) > 1e-6:
        vfilter, _ = ops.speed_filters(speed)
        if vfilter:
            parts.append(vfilter)

    if seg.reverse:
        parts.append("reverse")

    return parts


def _audio_filter_parts(seg: "Segment") -> list[str]:
    """Build the ordered ``-af`` fragments (atempo chain + optional areverse)."""
    parts: list[str] = []
    speed = _speed_f(seg)
    if abs(speed - 1.0) > 1e-6:
        _, afilter = ops.speed_filters(speed)
        if afilter:
            parts.append(afilter)
    if seg.reverse:
        parts.append("areverse")
    return parts


def render_segment(seg: "Segment", *, has_audio: bool | None = None) -> str:
    """Render *seg* to an mp4 and return its absolute path (cache-first).

    Computes ``sig = segment_render_sig(seg)`` and returns
    ``paths.cached_render_path(sig)`` immediately when that file already exists.
    Otherwise runs ONE ffmpeg pass — ``-ss in_ -i source -t src_len`` with the
    crop -> scale -> flip -> colour -> setpts -> reverse video chain and the
    atempo/areverse audio chain — encoding ``libx264 -crf 18 -pix_fmt yuv420p``.

    ``has_audio``: when ``None`` the source is probed; ``False`` forces ``-an``.
    Raises ``ffmpeg.FFmpegError`` on failure (the caller surfaces a Warning).
    """
    sig = segment_render_sig(seg)
    dest = paths.cached_render_path(sig)
    if os.path.exists(dest):
        return dest

    exe = _require_ffmpeg()
    src_len = _src_len(seg)
    if src_len <= 0.0:
        raise ffmpeg.FFmpegError("Segment has an empty trim window (out <= in).")

    if has_audio is None:
        has_audio = _probe_has_audio(seg.source)

    args: list[str] = [
        exe, "-y",
        "-ss", f"{float(seg.in_):.6f}",
        "-i", str(seg.source),
        "-t", f"{src_len:.6f}",
    ]

    vparts = _video_filter_parts(seg)
    if vparts:
        args += ["-vf", ",".join(vparts)]

    if has_audio:
        aparts = _audio_filter_parts(seg)
        if aparts:
            args += ["-af", ",".join(aparts)]

    args += list(_VENC)

    if has_audio:
        # Always re-encode audio when present: -ss/-t re-times the stream.
        args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]

    args += ["-movflags", "+faststart", dest]

    ffmpeg.run(args, timeout=900)
    return dest


# --------------------------------------------------------------------------- #
#  filmstrip (timeline clip background)                                        #
# --------------------------------------------------------------------------- #

def filmstrip_for(seg: "Segment", *, cols: int = 12) -> str:
    """Generate (cache-first) a horizontally-tiled filmstrip PNG for *seg*.

    Keyed on :func:`segment_render_sig` so a trim/edit regenerates it ->
    ``paths.cached_thumb_path(sig)``. One ffmpeg pass over the segment's source
    window samples ``cols`` evenly-spaced frames, scales them to 72px tall and
    tiles them into a ``cols x 1`` strip. The crop (when present) is baked in so a
    cropped clip previews the right region. Returns the abs path, or ``""`` on any
    failure (the timeline then falls back to a flat clip colour).
    """
    try:
        sig = segment_render_sig(seg)
        dest = paths.cached_thumb_path(sig)
        if os.path.exists(dest):
            return dest

        exe = ffmpeg.ffmpeg_path()
        if not exe:
            return ""

        src_len = _src_len(seg)
        if src_len <= 0.0:
            return ""

        # Sample fps = cols frames spread across the window; guard tiny windows so
        # ffmpeg always has at least `cols` source frames to tile.
        fps = max(0.1, float(cols) / src_len)

        vf_parts: list[str] = []
        if seg.crop:
            c = seg.crop
            frag = ops.crop_vf(c["x"], c["y"], c["w"], c["h"])
            if frag:
                vf_parts.append(frag)
        vf_parts.append(f"fps={fps:g}")
        vf_parts.append("scale=-1:72")
        vf_parts.append(f"tile={int(cols)}x1")

        args = [
            exe, "-y",
            "-ss", f"{float(seg.in_):.6f}",
            "-i", str(seg.source),
            "-t", f"{src_len:.6f}",
            "-vf", ",".join(vf_parts),
            "-frames:v", "1",
            dest,
        ]
        ffmpeg.run(args, timeout=120)
        return dest if os.path.exists(dest) else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
#  frame extraction (crop-canvas background)                                   #
# --------------------------------------------------------------------------- #

def extract_frame(seg: "Segment", at_src_sec: float | None = None) -> str:
    """Extract one UNTRIMMED, UN-CROPPED source frame as a PNG data-URI.

    Defaults to ``seg.in_``. The frame is the raw source frame so its
    ``naturalWidth x naturalHeight == seg.src_w x seg.src_h`` — exactly the
    source-pixel space the crop canvas emits coords in (no transform needed).
    Returns a ``data:image/png;base64,...`` string, or ``""`` on failure (the
    caller surfaces a Warning).
    """
    try:
        exe = ffmpeg.ffmpeg_path()
        if not exe:
            return ""
        at = float(seg.in_ if at_src_sec is None else at_src_sec)
        if at < 0.0:
            at = 0.0
        tmp = paths.work_path(".png")
        args = [
            exe, "-y",
            "-ss", f"{at:.6f}",
            "-i", str(seg.source),
            "-frames:v", "1",
            "-f", "image2",
            tmp,
        ]
        ffmpeg.run(args, timeout=120)
        if not os.path.exists(tmp):
            return ""
        with open(tmp, "rb") as fh:
            data = fh.read()
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if not data:
            return ""
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""
