"""One-shot ffmpeg edit operations for a single clip.

Two layers live here. The *pure* filter-string builders (``flip_vf``,
``scale_vf``, ``crop_vf``, ``speed_filters``, ``color_vf``) take plain numbers
and return an ffmpeg ``-vf`` / ``-af`` fragment with no I/O — easy to read and
test in isolation. The *appliers* (``apply_vf``, ``apply_flip``,
``apply_resize``, ``apply_crop``, ``apply_speed``, ``apply_color``) run ffmpeg
through ``core.ffmpeg`` and each return a brand-new working file from
``paths.work_path()`` so the caller can push it onto the undo stack and let
Gradio's content-hash refresh the preview.

Video is re-encoded ``libx264 -crf 18 -pix_fmt yuv420p -preset veryfast`` so the
edits stack cleanly; audio is stream-copied unless an op changes timing (speed),
in which case it is re-encoded to AAC. ``apply_speed`` tolerates sources with no
audio stream by simply omitting the ``-af``/audio output.
"""
from __future__ import annotations

from . import ffmpeg, paths

# Shared encoder options for the re-encoded video stream. Kept in one place so
# every applier produces byte-comparable settings (crf 18 is visually lossless
# for these single-pass edits).
_VENC = ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-preset", "veryfast"]


# --------------------------------------------------------------------------- #
#  pure filter-string builders (no ffmpeg call)                               #
# --------------------------------------------------------------------------- #

def flip_vf(horizontal: bool, vertical: bool) -> str:
    """``hflip`` / ``vflip`` / ``hflip,vflip`` for the requested axes; "" if none."""
    parts = []
    if horizontal:
        parts.append("hflip")
    if vertical:
        parts.append("vflip")
    return ",".join(parts)


def scale_vf(width: int | None, height: int | None) -> str:
    """A ``scale=W:H`` fragment; an unspecified dim becomes ``-2`` (keep even &
    preserve aspect, e.g. ``scale=1280:-2``). Explicit dims are rounded down to
    the nearest even value (min 2) because libx264 / yuv420p require even W and H
    — without this, an odd typed width like 321 makes ffmpeg fail. "" when both
    dims are unspecified."""
    w = int(width) if width else None
    h = int(height) if height else None
    if w is None and h is None:
        return ""
    sw = str(max(2, (w // 2) * 2)) if w is not None else "-2"
    sh = str(max(2, (h // 2) * 2)) if h is not None else "-2"
    return f"scale={sw}:{sh}"


def crop_vf(x: int, y: int, w: int, h: int) -> str:
    """A ``crop=w:h:x:y`` fragment (note ffmpeg's w:h:x:y ordering)."""
    return f"crop={int(w)}:{int(h)}:{int(x)}:{int(y)}"


def speed_filters(factor: float) -> tuple[str, str]:
    """``(video setpts, audio atempo chain)`` for a playback-speed change.

    ``factor`` > 1 speeds up. Video PTS is divided by the factor. ``atempo`` only
    accepts 0.5..2.0 per stage, so factors outside that window are realised by
    chaining stages (e.g. 4x -> ``atempo=2.0,atempo=2.0``; 0.25x ->
    ``atempo=0.5,atempo=0.5``). Returns ``("", "")`` for a no-op factor of 1.
    """
    s = float(factor)
    if abs(s - 1.0) < 1e-6 or s <= 0.0:
        return "", ""
    vfilter = f"setpts=PTS/{s:g}"
    stages: list[str] = []
    remaining = s
    guard = 0
    while remaining > 2.0 and guard < 16:
        stages.append("atempo=2.0")
        remaining /= 2.0
        guard += 1
    while remaining < 0.5 and guard < 16:
        stages.append("atempo=0.5")
        remaining /= 0.5
        guard += 1
    if abs(remaining - 1.0) > 1e-6:
        stages.append(f"atempo={remaining:g}")
    return vfilter, ",".join(stages)


def color_vf(brightness: float = 0.0, contrast: float = 1.0, saturation: float = 1.0,
             gamma: float = 1.0, hue: float = 0.0, warmth: float = 0.0) -> str:
    """A colour-grade fragment from neutral-anchored controls; "" if all neutral.

    ``eq`` carries brightness/contrast/saturation/gamma (emitted only when at
    least one is off-neutral), ``hue=h=<deg>`` rotates hue, and a
    ``colorchannelmixer`` applies white-balance warmth: ``temp = warmth/100``
    lifts red (``rr = 1 + 0.25*temp``) and drops blue (``bb = 1 - 0.25*temp``).
    Non-empty parts are comma-joined.
    """
    b = float(brightness)
    c = float(contrast)
    s = float(saturation)
    g = float(gamma)
    h = float(hue)
    warm = float(warmth)
    parts: list[str] = []
    if (abs(b) > 1e-6 or abs(c - 1.0) > 1e-6 or abs(s - 1.0) > 1e-6 or abs(g - 1.0) > 1e-6):
        parts.append(
            f"eq=brightness={b:.4f}:contrast={c:.4f}:saturation={s:.4f}:gamma={g:.4f}")
    if abs(h) > 1e-6:
        parts.append(f"hue=h={h:g}")
    if abs(warm) > 1e-6:
        temp = warm / 100.0
        rr = 1.0 + 0.25 * temp
        bb = 1.0 - 0.25 * temp
        parts.append(f"colorchannelmixer=rr={rr:.4f}:bb={bb:.4f}")
    return ",".join(parts)


# --------------------------------------------------------------------------- #
#  appliers (run ffmpeg, return a new working file)                           #
# --------------------------------------------------------------------------- #

def apply_vf(src: str, vf: str, audio: str = "copy") -> str:
    """Run a single ``-vf`` pass over ``src`` into a fresh working file.

    ``vf`` may be "" (the clip is simply re-encoded / remuxed). ``audio`` is
    ``"copy"`` (stream-copy) or ``"aac"`` (re-encode). Returns the new path.
    """
    dst = paths.work_path(".mp4")
    exe = ffmpeg.ffmpeg_path()
    if not exe:
        raise ffmpeg.FFmpegError("ffmpeg binary not found")
    args = [exe, "-y", "-i", src]
    if vf:
        args += ["-vf", vf]
    args += list(_VENC)
    if audio == "aac":
        args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-c:a", "copy"]
    args += ["-movflags", "+faststart", dst]
    ffmpeg.run(args)
    return dst


def apply_flip(src: str, horizontal: bool, vertical: bool) -> str:
    """Mirror the clip horizontally and/or vertically (audio copied)."""
    return apply_vf(src, flip_vf(horizontal, vertical), audio="copy")


def apply_resize(src: str, width: int | None, height: int | None) -> str:
    """Resize the clip; either dim may be ``None`` for auto (``-2``)."""
    return apply_vf(src, scale_vf(width, height), audio="copy")


def apply_crop(src: str, x: int, y: int, w: int, h: int) -> str:
    """Crop a ``w x h`` rectangle at offset ``(x, y)`` (audio copied)."""
    return apply_vf(src, crop_vf(x, y, w, h), audio="copy")


def apply_speed(src: str, factor: float) -> str:
    """Change playback speed; audio is retimed (atempo) and re-encoded to AAC.

    When ``src`` has no audio stream the ``-af``/audio output is omitted so the
    pass still succeeds. A factor of 1 is a plain re-encode passthrough.
    """
    dst = paths.work_path(".mp4")
    exe = ffmpeg.ffmpeg_path()
    if not exe:
        raise ffmpeg.FFmpegError("ffmpeg binary not found")
    vfilter, afilter = speed_filters(factor)

    has_audio = _has_audio(src)

    args = [exe, "-y", "-i", src]
    if vfilter:
        args += ["-vf", vfilter]
    args += list(_VENC)
    if has_audio:
        if afilter:
            args += ["-af", afilter]
        args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", dst]
    ffmpeg.run(args)
    return dst


def apply_color(src: str, brightness: float, contrast: float, saturation: float,
                gamma: float, hue: float, warmth: float) -> str:
    """Apply a colour grade (eq / hue / warmth) and re-encode (audio copied)."""
    vf = color_vf(brightness, contrast, saturation, gamma, hue, warmth)
    return apply_vf(src, vf, audio="copy")


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #

def _has_audio(src: str) -> bool:
    """Best-effort check for an audio stream via ``core.ffmpeg.probe``.

    Falls back to ``True`` if the probe can't tell — re-encoding a silent stream
    is harmless, whereas wrongly dropping ``-af`` would desync real audio.
    """
    try:
        info = ffmpeg.probe(src)
    except Exception:
        return True
    return bool(info.get("has_audio", True))
