"""The Mutator single-track edit model — pure Python, no Gradio, no ffmpeg.

A :class:`Track` is ONE ordered list of :class:`Segment`\\ s. The track starts as a
single segment spanning the whole source; SPLICE razors the selected segment at a
source-second into two halves that inherit every edit; REJOIN merges a segment with
a contiguous same-source neighbour whose edits match. Each segment carries its own
trim (``in_``/``out`` in source seconds) plus independent crop / resize / flip /
speed / reverse / colour edits, so selecting a segment loads ITS edits into the
tools and the Result player.

The track lives on the plugin instance (single-user state, like Reel2Reel's
``self._project``) — NOT in ``gr.State``. It serialises to/from the timeline
edit-JSON the browser bridge agrees on (:meth:`Track.to_json` / :meth:`Track.from_json`)
and keeps a bounded JSON undo/redo history (:meth:`Track.snapshot` / :meth:`Track.restore`).

Probe info is passed IN to :meth:`Track.load_source` so this module never imports
ffmpeg; the render-bookkeeping fields (``render_path``/``render_sig``/``thumb_path``)
are filled by ``core.render`` and never trusted across a reload.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
#  Constants                                                                    #
# --------------------------------------------------------------------------- #

# Cap on the undo history; older snapshots drop from the bottom (carried from
# clipstate.py).
_UNDO_CAP = 30

# Colour neutral anchors, in UI slider units. The MODEL stores UI units; the
# render layer maps these to ffmpeg units (brightness=(ui-100)/100, contrast=ui/100,
# saturation=ui/100, gamma/hue/warmth raw). A colour dict equal to this is "neutral"
# and contributes no filter.
COLOUR_NEUTRAL: dict[str, float] = {
    "brightness": 100,   # slider 50..150
    "contrast":   100,   # slider 50..150
    "saturation": 100,   # slider 0..200
    "hue":        0,     # slider -180..180
    "warmth":     0,     # slider -100..100
    "gamma":      1.0,   # slider 0.5..2
}

# Each colour key's (lo, hi) slider range, used to clamp inbound values.
_COLOUR_RANGES: dict[str, tuple[float, float]] = {
    "brightness": (50.0, 150.0),
    "contrast":   (50.0, 150.0),
    "saturation": (0.0, 200.0),
    "hue":        (-180.0, 180.0),
    "warmth":     (-100.0, 100.0),
    "gamma":      (0.5, 2.0),
}

# Speed clamp (matches set_edit / from_json sanitisation).
_SPEED_LO = 0.1
_SPEED_HI = 8.0

# Module-level id counter (mirrors Reel2Reel/core/timeline.new_id).
_id_counter = 0


def new_id(prefix: str = "s") -> str:
    """Return a fresh, process-unique segment id like ``s1``, ``s2``, …."""
    global _id_counter
    _id_counter += 1
    return f"{prefix}{_id_counter}"


# --------------------------------------------------------------------------- #
#  Numeric helpers                                                             #
# --------------------------------------------------------------------------- #

def _f(v, default: float = 0.0, lo: float = -1e9, hi: float = 1e9) -> float:
    """Sanitize a float: reject NaN/inf, clamp to a sane range."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return max(lo, min(hi, x))


def _i(v, default: int = 0) -> int:
    """Sanitize an int via the float sanitiser, then round."""
    return int(round(_f(v, float(default))))


def _even(n: int) -> int:
    """Round ``n`` down to the nearest even integer (>= 0) for libx264."""
    n = int(n)
    if n < 0:
        n = 0
    return n - (n % 2)


def _clamp_colour(colour: dict | None) -> dict:
    """Return a full colour dict with every key present and clamped to range."""
    out = dict(COLOUR_NEUTRAL)
    if isinstance(colour, dict):
        for k, (lo, hi) in _COLOUR_RANGES.items():
            if k in colour and colour[k] is not None:
                out[k] = _f(colour[k], COLOUR_NEUTRAL[k], lo, hi)
    return out


def _clamp_crop(crop: dict | None, src_w: int, src_h: int) -> Optional[dict]:
    """Round a crop rect to even-dimensioned source-pixel integers, clamped to the
    source frame. Returns ``None`` for a missing/degenerate rect."""
    if not isinstance(crop, dict):
        return None
    sw = int(src_w) if src_w and src_w > 0 else 1_000_000
    sh = int(src_h) if src_h and src_h > 0 else 1_000_000
    x = max(0, min(sw, _i(crop.get("x", 0))))
    y = max(0, min(sh, _i(crop.get("y", 0))))
    w = _even(max(0, min(sw - x, _i(crop.get("w", 0)))))
    h = _even(max(0, min(sh - y, _i(crop.get("h", 0)))))
    if w < 2 or h < 2:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _clamp_resize(resize: dict | None, lock_aspect: bool, src_w: int, src_h: int) -> Optional[dict]:
    """Round a resize target to even ints. Either dim may be ``None`` (auto). When
    ``lock_aspect`` and exactly one dim is given, derive the other from the source
    aspect. Returns ``None`` when no usable target dim remains."""
    if not isinstance(resize, dict):
        return None
    rw = resize.get("w")
    rh = resize.get("h")
    w = _even(_i(rw)) if rw not in (None, "") else None
    h = _even(_i(rh)) if rh not in (None, "") else None
    if w is not None and w < 2:
        w = None
    if h is not None and h < 2:
        h = None
    if lock_aspect and src_w and src_h:
        if w is not None and h is None:
            h = _even(int(round(w * src_h / src_w)))
        elif h is not None and w is None:
            w = _even(int(round(h * src_w / src_h)))
    if w is None and h is None:
        return None
    return {"w": w, "h": h}


# --------------------------------------------------------------------------- #
#  Segment                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    """One clip on the single track — a trimmed window of a source plus per-clip edits."""

    id: str
    source: str                       # absolute source path (the REAL file; Save-in-place target)
    src_fps: float = 0.0
    src_w: int = 0
    src_h: int = 0
    in_: float = 0.0                  # source seconds (trim in)
    out: float = 0.0                  # source seconds (trim out)
    label: str = ""
    # per-clip edits:
    crop: Optional[dict] = None       # {x, y, w, h} in SOURCE px (even w/h) or None
    resize: Optional[dict] = None     # {w, h} target px (either may be None for auto) or None
    lock_aspect: bool = True
    flip_h: bool = False
    flip_v: bool = False
    speed: float = 1.0                # >0; 1.0 = no change
    reverse: bool = False
    colour: dict = field(default_factory=lambda: dict(COLOUR_NEUTRAL))
    # render bookkeeping (filled by core.render, never trusted across reload):
    render_path: Optional[str] = None
    render_sig: Optional[str] = None
    thumb_path: Optional[str] = None  # filmstrip PNG (server-side abs path)

    # -- derived ------------------------------------------------------------
    @property
    def speed_f(self) -> float:
        s = float(self.speed or 1.0)
        return s if s > 0.01 else 1.0

    @property
    def src_len(self) -> float:
        """Seconds consumed from the source (the trim length)."""
        return max(0.0, float(self.out) - float(self.in_))

    @property
    def dur(self) -> float:
        """On-timeline length = source length scaled by speed."""
        return self.src_len / self.speed_f

    @property
    def is_neutral_colour(self) -> bool:
        """True when every colour key equals its neutral anchor."""
        c = self.colour or {}
        for k, v in COLOUR_NEUTRAL.items():
            if abs(_f(c.get(k, v), v) - float(v)) > 1e-6:
                return False
        return True

    @property
    def has_edits(self) -> bool:
        """True when any crop/resize/flip/reverse is set, speed != 1, or colour off-neutral."""
        return bool(
            self.crop
            or self.resize
            or self.flip_h
            or self.flip_v
            or self.reverse
            or abs(self.speed_f - 1.0) > 1e-6
            or not self.is_neutral_colour
        )

    def _invalidate_render(self) -> None:
        """Drop cached render/thumb bookkeeping after any edit that changes the pixels."""
        self.render_path = None
        self.render_sig = None
        self.thumb_path = None


# --------------------------------------------------------------------------- #
#  Track                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Track:
    """The single ordered track of segments plus selection, playhead and undo history."""

    segments: list[Segment] = field(default_factory=list)
    selected_id: Optional[str] = None
    playhead: float = 0.0             # timeline seconds
    px_per_sec: float = 80.0
    snap: bool = True
    # source info of the most-recently loaded source (for the title/info readout):
    source: str = ""
    src_fps: float = 0.0
    src_w: int = 0
    src_h: int = 0
    has_audio: bool = False
    _undo: list[str] = field(default_factory=list)   # JSON snapshots, cap 30
    _redo: list[str] = field(default_factory=list)

    # ---- frame math -------------------------------------------------------
    def fps(self) -> float:
        """FPS for frame math (`1/fps` trim clamps). Prefer the track source's fps,
        then the selected segment's, else 30."""
        if self.src_fps and self.src_fps > 0:
            return float(self.src_fps)
        sel = self.selected()
        if sel is not None and sel.src_fps and sel.src_fps > 0:
            return float(sel.src_fps)
        return 30.0

    # ---- loading ----------------------------------------------------------
    def load_source(self, path: str, probe_info: dict) -> Segment:
        """Reset the track to ONE segment spanning the whole source.

        ``probe_info`` is the dict from ``core.ffmpeg.probe``. Does NOT push undo
        (the caller decides). Returns the new segment.
        """
        self._set_source_info(path, probe_info)
        seg = self._make_source_segment(path, probe_info, label="Clip 1")
        self.segments = [seg]
        self.selected_id = seg.id
        self.playhead = 0.0
        return seg

    def append_source(self, path: str, probe_info: dict) -> Segment:
        """Append a whole-source segment to the END of the track (extend, don't replace).

        Used for a SendTo hand-off that should extend an existing track. Selects the
        new segment and returns it.
        """
        self._set_source_info(path, probe_info)
        seg = self._make_source_segment(
            path, probe_info, label=f"Clip {len(self.segments) + 1}"
        )
        self.segments.append(seg)
        self.selected_id = seg.id
        return seg

    def _set_source_info(self, path: str, probe_info: dict) -> None:
        info = probe_info or {}
        self.source = str(path)
        self.src_fps = _f(info.get("fps", 0.0))
        self.src_w = _i(info.get("width", 0))
        self.src_h = _i(info.get("height", 0))
        self.has_audio = bool(info.get("has_audio", False))

    def _make_source_segment(self, path: str, probe_info: dict, *, label: str) -> Segment:
        info = probe_info or {}
        return Segment(
            id=new_id(),
            source=str(path),
            src_fps=_f(info.get("fps", 0.0)),
            src_w=_i(info.get("width", 0)),
            src_h=_i(info.get("height", 0)),
            in_=0.0,
            out=max(0.0, _f(info.get("duration", 0.0))),
            label=label,
        )

    # ---- selection --------------------------------------------------------
    def select(self, seg_id: str) -> Optional[Segment]:
        """Select ``seg_id`` if present (else leave the selection unchanged)."""
        if any(s.id == seg_id for s in self.segments):
            self.selected_id = seg_id
        return self.selected()

    def selected(self) -> Optional[Segment]:
        """The selected segment. If ``selected_id`` is stale but segments exist,
        re-point it to the first segment."""
        if not self.segments:
            return None
        for s in self.segments:
            if s.id == self.selected_id:
                return s
        self.selected_id = self.segments[0].id
        return self.segments[0]

    def index_of(self, seg_id: str) -> int:
        """Index of ``seg_id`` in :attr:`segments`, or ``-1`` if absent."""
        for i, s in enumerate(self.segments):
            if s.id == seg_id:
                return i
        return -1

    def reflow(self) -> None:
        """No-op hook. Timeline ``start`` is derived (running sum of prior ``dur``) at
        :meth:`to_json` time, so nothing persisted needs recomputing. Kept callable for
        future-proofing."""
        return None

    # ---- splice / rejoin --------------------------------------------------
    def splice(self, seg_id: str, at_src_sec: float) -> list[str]:
        """Razor ``seg_id`` at ``at_src_sec`` (a SOURCE-second within ``[in_, out]``).

        The two halves inherit ALL edits. Returns ``[head_id, tail_id]``, or ``[]``
        (no-op) when the cut is too close to either edge.
        """
        idx = self.index_of(seg_id)
        if idx < 0:
            return []
        orig = self.segments[idx]
        minf = 1.0 / max(1.0, self.fps())
        at = _f(at_src_sec)
        if at < orig.in_ + minf or at > orig.out - minf:
            return []

        base_label = orig.label or "Clip"
        head = self._clone_segment(orig, in_=orig.in_, out=at, label=f"{base_label} a")
        tail = self._clone_segment(orig, in_=at, out=orig.out, label=f"{base_label} b")
        self.segments[idx:idx + 1] = [head, tail]
        self.selected_id = head.id
        return [head.id, tail.id]

    def _clone_segment(self, orig: Segment, *, in_: float, out: float, label: str) -> Segment:
        """A fresh-id copy of ``orig`` with a new trim window, deep-copying mutable edits."""
        return Segment(
            id=new_id(),
            source=orig.source,
            src_fps=orig.src_fps,
            src_w=orig.src_w,
            src_h=orig.src_h,
            in_=float(in_),
            out=float(out),
            label=label,
            crop=copy.deepcopy(orig.crop),
            resize=copy.deepcopy(orig.resize),
            lock_aspect=orig.lock_aspect,
            flip_h=orig.flip_h,
            flip_v=orig.flip_v,
            speed=orig.speed,
            reverse=orig.reverse,
            colour=copy.deepcopy(orig.colour),
        )

    def rejoin(self, seg_id: str) -> bool:
        """Merge ``seg_id`` with a contiguous same-source neighbour whose edits match.

        Prefers the RIGHT neighbour, else the LEFT. On merge the left segment's ``out``
        extends to the right segment's ``out``, the right is dropped, the left keeps its
        id + edits and stays selected. Returns ``True`` on a merge, else ``False``.
        """
        idx = self.index_of(seg_id)
        if idx < 0:
            return False
        seg = self.segments[idx]

        right = self.segments[idx + 1] if idx + 1 < len(self.segments) else None
        left = self.segments[idx - 1] if idx - 1 >= 0 else None

        # Prefer the right neighbour, else the left.
        if right is not None and self._mergeable(seg, right):
            keep_idx, drop_idx = idx, idx + 1
        elif left is not None and self._mergeable(left, seg):
            keep_idx, drop_idx = idx - 1, idx
        else:
            return False

        keep = self.segments[keep_idx]
        drop = self.segments[drop_idx]
        keep.out = drop.out
        del self.segments[drop_idx]
        keep._invalidate_render()
        self.selected_id = keep.id
        return True

    def _mergeable(self, left: Segment, right: Segment) -> bool:
        """True when ``left`` and ``right`` are the same source, source-contiguous
        (``left.out`` meets ``right.in_``), and every edit matches."""
        if left.source != right.source:
            return False
        minf = 1.0 / max(1.0, self.fps())
        if abs(float(left.out) - float(right.in_)) >= minf:
            return False
        return (
            left.crop == right.crop
            and left.resize == right.resize
            and bool(left.lock_aspect) == bool(right.lock_aspect)
            and bool(left.flip_h) == bool(right.flip_h)
            and bool(left.flip_v) == bool(right.flip_v)
            and abs(left.speed_f - right.speed_f) < 1e-9
            and bool(left.reverse) == bool(right.reverse)
            and _clamp_colour(left.colour) == _clamp_colour(right.colour)
        )

    # ---- trim / edits -----------------------------------------------------
    def trim(self, seg_id: str, in_: float, out: float,
             src_dur: float | None = None) -> None:
        """Set the segment's trim window (source seconds), clamped to a valid range.

        Clamps ``0 <= in_`` and ``in_ + 1/fps <= out``; when ``src_dur`` is given (and
        positive) it is the hard ceiling for ``out``. Invalidates the segment's cached
        render/thumb.
        """
        idx = self.index_of(seg_id)
        if idx < 0:
            return
        seg = self.segments[idx]
        minf = 1.0 / max(1.0, self.fps())
        ni = max(0.0, _f(in_))
        no = _f(out, ni + minf)
        if src_dur is not None and src_dur > 0:
            no = min(no, float(src_dur))
            ni = min(ni, max(0.0, float(src_dur) - minf))
        no = max(ni + minf, no)
        seg.in_ = ni
        seg.out = no
        seg._invalidate_render()

    def set_edit(self, seg_id: str, src_dur: float | None = None, **params) -> None:
        """Apply one or more per-clip edit params to ``seg_id``.

        Accepted keys: ``crop`` (dict|None), ``resize`` (dict|None), ``lock_aspect``,
        ``flip_h``, ``flip_v``, ``speed`` (clamped 0.1..8.0), ``reverse``, the six colour
        keys individually (merged into ``seg.colour``) OR a whole ``colour=dict``, and the
        trim keys ``in_``/``out`` (clamped against ``src_dur`` when given). Invalidates the
        segment's cached render/thumb after any change.
        """
        idx = self.index_of(seg_id)
        if idx < 0:
            return
        seg = self.segments[idx]

        # lock_aspect first so a same-call resize honours the new lock state.
        if "lock_aspect" in params:
            seg.lock_aspect = bool(params["lock_aspect"])

        if "crop" in params:
            seg.crop = _clamp_crop(params["crop"], seg.src_w, seg.src_h)

        if "resize" in params:
            seg.resize = _clamp_resize(params["resize"], seg.lock_aspect, seg.src_w, seg.src_h)

        if "flip_h" in params:
            seg.flip_h = bool(params["flip_h"])
        if "flip_v" in params:
            seg.flip_v = bool(params["flip_v"])
        if "reverse" in params:
            seg.reverse = bool(params["reverse"])

        if "speed" in params:
            seg.speed = _f(params["speed"], 1.0, _SPEED_LO, _SPEED_HI)

        # Whole-colour replacement, then individual key merges.
        if "colour" in params:
            seg.colour = _clamp_colour(params["colour"])
        indiv = {k: params[k] for k in _COLOUR_RANGES if k in params}
        if indiv:
            merged = dict(seg.colour or COLOUR_NEUTRAL)
            merged.update(indiv)
            seg.colour = _clamp_colour(merged)

        # Trim keys (in_ / out) routed through the same clamps as trim().
        if "in_" in params or "out" in params:
            ni = params.get("in_", seg.in_)
            no = params.get("out", seg.out)
            minf = 1.0 / max(1.0, self.fps())
            ni = max(0.0, _f(ni))
            no = _f(no, ni + minf)
            if src_dur is not None and src_dur > 0:
                no = min(no, float(src_dur))
                ni = min(ni, max(0.0, float(src_dur) - minf))
            seg.in_ = ni
            seg.out = max(ni + minf, no)

        seg._invalidate_render()

    # ---- serialisation ----------------------------------------------------
    def to_json(self) -> dict:
        """Serialise to the timeline edit-JSON schema (§4.2).

        Computes each segment's running ``start`` and ``dur``. ``source`` /
        ``render_path`` / ``thumb_url`` are emitted RAW (server-side abs paths); the
        plugin rewrites ``render_path`` / ``thumb_url`` to ``/gradio_api/file=…`` URLs
        before sending. The model never builds URLs.
        """
        fps = self.fps()
        segs: list[dict] = []
        running = 0.0
        for s in self.segments:
            dur = s.dur
            segs.append({
                "id": s.id,
                "source": s.source,
                "label": s.label,
                "src_fps": round(float(s.src_fps), 6),
                "src_w": int(s.src_w),
                "src_h": int(s.src_h),
                "start": round(running, 4),
                "in": round(float(s.in_), 4),
                "out": round(float(s.out), 4),
                "src_len": round(s.src_len, 4),
                "dur": round(dur, 4),
                "crop": copy.deepcopy(s.crop),
                "resize": copy.deepcopy(s.resize),
                "lock_aspect": bool(s.lock_aspect),
                "flip_h": bool(s.flip_h),
                "flip_v": bool(s.flip_v),
                "speed": round(s.speed_f, 4),
                "reverse": bool(s.reverse),
                "colour": dict(_clamp_colour(s.colour)),
                "graded": not s.is_neutral_colour,
                "render_path": s.render_path,
                "thumb_url": s.thumb_path,
            })
            running += dur
        return {
            "fps": round(float(fps), 6),
            "selected_id": self.selected_id,
            "playhead": round(float(self.playhead), 4),
            "segments": segs,
            "ui": {
                "px_per_sec": round(float(self.px_per_sec), 4),
                "playhead": round(float(self.playhead), 4),
                "selected": self.selected_id,
                "snap": bool(self.snap),
            },
        }

    def from_json(self, d: dict) -> None:
        """Rebuild segments from an inbound browser payload (§4.2).

        Re-clamps every value via the same rules as :meth:`set_edit`. Preserves the
        server-only render-bookkeeping fields by id-matching against the CURRENT
        segments where the source + trim + edits are unchanged, so an unrelated
        selection/playhead change never drops a valid cached render. Segments arriving
        in a new order are taken in array order.
        """
        d = d or {}
        prior = {s.id: s for s in self.segments}

        new_segs: list[Segment] = []
        for cd in d.get("segments", []):
            if not isinstance(cd, dict):
                continue
            new_segs.append(self._segment_from_dict(cd, prior))
        self.segments = new_segs

        ui = d.get("ui") if isinstance(d.get("ui"), dict) else {}
        sel = ui.get("selected", d.get("selected_id"))
        self.selected_id = sel if any(s.id == sel for s in self.segments) else (
            self.segments[0].id if self.segments else None
        )
        self.playhead = max(0.0, _f(ui.get("playhead", d.get("playhead", self.playhead))))
        self.px_per_sec = _f(ui.get("px_per_sec", self.px_per_sec), 80.0, 1.0, 4000.0)
        if "snap" in ui:
            self.snap = bool(ui["snap"])
        elif "snap" in d:
            self.snap = bool(d["snap"])

    def _segment_from_dict(self, cd: dict, prior: dict[str, "Segment"]) -> Segment:
        """Build one sanitised Segment from a browser payload dict, preserving a cached
        render from a matching prior segment when the pixels are unchanged."""
        sid = cd.get("id") or new_id()
        src_fps = _f(cd.get("src_fps", 0.0))
        src_w = _i(cd.get("src_w", 0))
        src_h = _i(cd.get("src_h", 0))
        # fps for the per-clip 1/fps clamp: prefer the clip's own, else the track's.
        cfps = src_fps if src_fps > 0 else self.fps()
        minf = 1.0 / max(1.0, cfps)

        in_ = max(0.0, _f(cd.get("in", cd.get("in_", 0.0))))
        out = max(in_ + minf, _f(cd.get("out", in_ + minf)))
        lock_aspect = bool(cd.get("lock_aspect", True))

        seg = Segment(
            id=sid,
            source=str(cd.get("source", "")),
            src_fps=src_fps,
            src_w=src_w,
            src_h=src_h,
            in_=in_,
            out=out,
            label=cd.get("label", ""),
            crop=_clamp_crop(cd.get("crop"), src_w, src_h),
            resize=_clamp_resize(cd.get("resize"), lock_aspect, src_w, src_h),
            lock_aspect=lock_aspect,
            flip_h=bool(cd.get("flip_h", False)),
            flip_v=bool(cd.get("flip_v", False)),
            speed=_f(cd.get("speed", 1.0), 1.0, _SPEED_LO, _SPEED_HI),
            reverse=bool(cd.get("reverse", False)),
            colour=_clamp_colour(cd.get("colour")),
        )
        # Carry a still-valid cached render/thumb forward from the matching prior
        # segment (same id, identical pixels) so a pure selection/playhead change
        # does not force a re-render.
        old = prior.get(sid)
        if old is not None and self._same_pixels(old, seg):
            seg.render_path = old.render_path
            seg.render_sig = old.render_sig
            seg.thumb_path = old.thumb_path
        return seg

    @staticmethod
    def _same_pixels(a: Segment, b: Segment) -> bool:
        """True when two segments would render identical pixels/audio (source + trim +
        every edit equal). Used to preserve a cached render across a JSON round-trip."""
        return (
            a.source == b.source
            and abs(float(a.in_) - float(b.in_)) < 1e-6
            and abs(float(a.out) - float(b.out)) < 1e-6
            and a.crop == b.crop
            and a.resize == b.resize
            and bool(a.lock_aspect) == bool(b.lock_aspect)
            and bool(a.flip_h) == bool(b.flip_h)
            and bool(a.flip_v) == bool(b.flip_v)
            and abs(a.speed_f - b.speed_f) < 1e-9
            and bool(a.reverse) == bool(b.reverse)
            and _clamp_colour(a.colour) == _clamp_colour(b.colour)
        )

    # ---- diff / undo ------------------------------------------------------
    def content_sig(self) -> str:
        """Stable signature for undo/diff that EXCLUDES selection / playhead / px_per_sec /
        snap and the render bookkeeping. A pure selection or playhead change must NOT change
        this (so it never forks an undo state). Mirrors Reel2Reel ``_sig_of``."""
        doc = [self._edit_doc(s) for s in self.segments]
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _edit_doc(s: Segment) -> dict:
        """The pixel-affecting fields of a segment (no id, no ui, no render bookkeeping)."""
        return {
            "source": s.source,
            "in": round(float(s.in_), 6),
            "out": round(float(s.out), 6),
            "crop": s.crop,
            "resize": s.resize,
            "lock_aspect": bool(s.lock_aspect),
            "flip_h": bool(s.flip_h),
            "flip_v": bool(s.flip_v),
            "speed": round(s.speed_f, 6),
            "reverse": bool(s.reverse),
            "colour": _clamp_colour(s.colour),
        }

    def _document(self) -> str:
        """The FULL undo document — segments (incl. id + edits) plus selected_id, so a
        restore brings back the selection too."""
        return json.dumps({
            "segments": [asdict(s) for s in self.segments],
            "selected_id": self.selected_id,
        }, separators=(",", ":"))

    def snapshot(self) -> None:
        """Push the current full document onto the undo stack (cap 30) and clear redo."""
        self._undo.append(self._document())
        if len(self._undo) > _UNDO_CAP:
            del self._undo[: len(self._undo) - _UNDO_CAP]
        self._redo.clear()

    def restore(self, doc_json: str) -> None:
        """Replace segments + selection from a snapshot document (used by undo/redo)."""
        try:
            doc = json.loads(doc_json)
        except (TypeError, ValueError):
            return
        segs: list[Segment] = []
        for sd in doc.get("segments", []):
            if not isinstance(sd, dict):
                continue
            sd = {**sd}
            # Tolerate a legacy "in"/"in_" key and drop unknown keys.
            if "in_" not in sd and "in" in sd:
                sd["in_"] = sd.pop("in")
            fields = Segment.__dataclass_fields__
            kwargs = {k: v for k, v in sd.items() if k in fields}
            kwargs.setdefault("id", new_id())
            kwargs.setdefault("source", "")
            kwargs["colour"] = _clamp_colour(kwargs.get("colour"))
            segs.append(Segment(**kwargs))
        self.segments = segs
        sel = doc.get("selected_id")
        self.selected_id = sel if any(s.id == sel for s in segs) else (
            segs[0].id if segs else None
        )

    def undo(self) -> bool:
        """Step back one edit state. Returns ``False`` (no-op) when the undo stack is empty."""
        if not self._undo:
            return False
        self._redo.append(self._document())
        if len(self._redo) > _UNDO_CAP:
            del self._redo[: len(self._redo) - _UNDO_CAP]
        self.restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        """Step forward one edit state. Returns ``False`` (no-op) when the redo stack is empty."""
        if not self._redo:
            return False
        self._undo.append(self._document())
        if len(self._undo) > _UNDO_CAP:
            del self._undo[: len(self._undo) - _UNDO_CAP]
        self.restore(self._redo.pop())
        return True

    def can_undo(self) -> bool:
        """True when there is a prior state to step back to."""
        return bool(self._undo)

    def can_redo(self) -> bool:
        """True when there is an undone state to step forward to."""
        return bool(self._redo)
