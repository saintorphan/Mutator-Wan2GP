"""Mutator — a Wan2GP plugin.

A single-clip video editor rendered as one main-webui tab ("Mutator"). Load the
clip selected in the preview gallery, upload one, or receive a SendTo hand-off,
then trim / split on a frame-accurate mini-timeline, crop, resize, flip, change
speed and colour-correct — each edit writing a fresh working file (so Gradio's
content-hash refreshes the preview) onto an undo stack. Finally Save in place
(overwrite the real source) or Save as copy (new file in the outputs folder), and
optionally send a frame or the whole edited clip onward via SendTo.

It composites/transforms an *existing* clip with ffmpeg only — it never generates
frames, so it needs none of the host's submit_task / model machinery. Editor
state (the open clip + undo/redo) lives on this single per-process plugin
instance (``self._clip``), like the sibling plugins; the per-session ``state``
dict is used only for the SendTo inbox hand-off. Single-user / local use.

NOTE: not an official plugin. Distribute via the plugin manager.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import types
from pathlib import Path

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin

from .core import clipstate, ffmpeg, inbox, ops, paths, trim
from .ui import filmstrip, logo, styles, suite

try:  # host ships gradio_rangeslider (used in wgp.py); two plain sliders otherwise
    from gradio_rangeslider import RangeSlider  # noqa: F401
    _HAVE_RANGESLIDER = True
except Exception:  # pragma: no cover
    RangeSlider = None
    _HAVE_RANGESLIDER = False

PLUGIN_ID = "Mutator"
PLUGIN_NAME = "Mutator"

_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v", ".gif")


def _fmt_info(info: "clipstate.ClipInfo") -> str:
    """One-line clip metadata banner (flags an estimated fps like Trimline)."""
    fps = info.fps or 0
    fps_str = f"~{fps:.3f} (estimated)" if info.fps_inexact else f"{fps:.3f}"
    audio = "🔊 audio" if info.has_audio else "🔇 no audio"
    return (f"**FPS:** {fps_str}  ·  **Frames:** {info.num_frames}  ·  "
            f"**{info.duration:.2f}s**  ·  {info.width}×{info.height}  ·  {audio}")


def _fmt_time(start_f: int, end_f: int, fps: float) -> str:
    """In/out summary for the trim range."""
    if fps <= 0:
        return ""
    s = trim.frame_to_sec(start_f, fps)
    e = trim.end_frame_to_sec(end_f, fps)
    return (f"In **{s:.3f}s** (f{start_f})  →  Out **{e:.3f}s** (f{end_f})  ·  "
            f"**{max(e - s, 0):.3f}s** · {end_f - start_f + 1} frames")


class Mutator(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PLUGIN_NAME
        try:
            self.version = json.loads(
                (Path(__file__).parent / "plugin_info.json").read_text()
            ).get("version", "0.1.0")
        except Exception:
            self.version = "0.1.0"
        self.description = ("Single-clip video editor: trim, split, crop, resize, "
                            "flip, speed and colour-correct a clip frame-accurately "
                            "with undo/redo, then save in place or as a copy.")
        # The clip currently open in the editor + its undo/redo history. Held on the
        # single per-process instance (single-user/local), mirroring Reel2Reel's
        # self._project. The per-session `state` dict carries only the inbox.
        self._clip = clipstate.ClipSession()
        self._c: dict = {}
        # "Send edited clip" target registry + SendTo enqueue fn (set in create_ui).
        self._clip_targets_by_label: dict = {}
        self._sendto_enqueue = None

    # -- inbox alias --------------------------------------------------------
    @staticmethod
    def _register_inbox_alias():
        """Let the SendTo "path" sender contract work regardless of the on-disk
        folder name: register ``mutator`` + ``mutator.inbox`` in sys.modules so
        ``from mutator.inbox import enqueue_clips`` resolves. The state-key
        fallback (writing ``state['mutator_inbox']`` directly) works without it.
        Idempotent and fully guarded."""
        try:
            alias = sys.modules.get("mutator")
            if alias is None:
                alias = types.ModuleType("mutator")
                sys.modules["mutator"] = alias
            alias.inbox = inbox
            alias.enqueue_clips = inbox.enqueue_clips
            sys.modules.setdefault("mutator.inbox", inbox)
        except Exception:
            traceback.print_exc()

    # -- lifecycle ----------------------------------------------------------
    def setup_ui(self):
        self._register_inbox_alias()
        paths.prune_cache()  # reclaim stale working files from earlier sessions

        self.request_component("state")
        self.request_component("output")
        self.request_component("main_tabs")
        self.request_component("refresh_form_trigger")
        self.request_component("gallery_tabs")
        self.request_component("current_gallery_tab")
        self.request_global("save_path")
        self.request_global("get_current_model_settings")
        self.request_global("get_video_info")

        self.add_tab(tab_id=PLUGIN_ID, label=PLUGIN_NAME,
                     component_constructor=self.create_ui)

    # -- host-state helpers -------------------------------------------------
    def _save_path(self) -> str:
        return getattr(self, "save_path", None) or "outputs"

    def _gvi(self):
        """The host's get_video_info, used as a probe fallback (None if absent)."""
        return getattr(self, "get_video_info", None)

    def _current_selection_path(self, state):
        """Absolute path of the clip selected in the preview gallery, or None.
        Reads the host gen bookkeeping ``gen['file_list'][gen['selected']]``."""
        gen = (state or {}).get("gen", {}) or {}
        files = gen.get("file_list") or []
        if not files:
            return None
        idx = gen.get("selected", 0) or 0
        if idx < 0 or idx >= len(files):
            idx = 0
        return files[idx]

    @staticmethod
    def _resolve(path):
        """Make a relative gallery/config path absolute so a later save isn't
        cwd-dependent."""
        if path and not os.path.isabs(path):
            cand = os.path.join(os.getcwd(), path)
            return cand if os.path.exists(cand) else path
        return path

    def _safe_frame(self, path, n):
        """A still PIL frame at index *n* via the host helper (None on failure)."""
        if not path:
            return None
        try:
            from shared.utils.utils import get_video_frame
            return get_video_frame(path, int(max(0, n)),
                                   return_last_if_missing=True, return_PIL=True)
        except Exception:
            traceback.print_exc()
            return None

    # -- range-slider adapter (RangeSlider vs two plain sliders) ------------
    def _trim_comps(self) -> list:
        """The active trim slider component(s) in wiring order."""
        c = self._c
        if _HAVE_RANGESLIDER and "trim_range" in c:
            return [c["trim_range"]]
        return [c["trim_start"], c["trim_end"]]

    def _read_range(self, range_args) -> tuple[int, int]:
        """Normalise the trailing slider input(s) into a sorted (start, end)."""
        if _HAVE_RANGESLIDER and "trim_range" in self._c:
            val = range_args[0]
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                lo, hi = int(round(val[0])), int(round(val[1]))
            else:
                lo = hi = 0
        else:
            lo, hi = int(round(range_args[0])), int(round(range_args[1]))
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def _range_update(self, start, end, maxv) -> list:
        """Slider update(s) sized to match the active slider component count."""
        if _HAVE_RANGESLIDER and "trim_range" in self._c:
            return [gr.update(minimum=0, maximum=max(maxv, 1), value=(start, end))]
        return [gr.update(minimum=0, maximum=max(maxv, 1), value=start),
                gr.update(minimum=0, maximum=max(maxv, 1), value=end)]

    # -- UI -----------------------------------------------------------------
    def create_ui(self, api_session=None):
        # api_session: zero-arg on the local host, a session on the newer API host.
        # Mutator is ffmpeg-only, so it is unused.
        self._api = api_session
        with gr.Column(elem_id="mutator-root"):
            # -- tab accent + logo banner (banner first, at the very top) -----
            # Each sibling plugin uses a distinct accent colour: a <style> plus a
            # tiny JS tagger that finds the tab button by its label and adds the
            # .mutator-tabbtn class. Then the logo banner, sized/positioned like
            # Image Suite's (top of the tab, content lines up beneath it).
            gr.HTML(f"<style>{styles.CSS}</style>", elem_classes="mutator-hidden")
            gr.HTML(
                "<img src=x style='display:none' onerror=\"(function(){"
                "var NAME=" + repr(PLUGIN_NAME) + ";"
                "function mark(){document.querySelectorAll("
                "'.tab-nav button,button[role=&quot;tab&quot;]').forEach(function(b){"
                "if(b.textContent.trim()===NAME)b.classList.add('mutator-tabbtn');});}"
                "mark();new MutationObserver(mark).observe(document.body,"
                "{childList:true,subtree:true});})()\">",
                elem_classes="mutator-hidden")
            gr.HTML(logo.banner_html())

            c = suite.build_ui(_HAVE_RANGESLIDER)
            self._c = c

            # -- embedded SendTo: FRAME send panel ---------------------------
            # Sends the still at the current PLAYHEAD frame to any "image" target.
            try:
                from sendto.embed import build_send_panel

                def _frame_at(playhead):
                    cur = self._clip.current
                    if cur is None:
                        return None
                    return self._safe_frame(cur.path, int(playhead or 0))

                build_send_panel(
                    state=self.state, main_tabs=self.main_tabs,
                    image_inputs=[c["playhead"]],
                    to_path=_frame_at,
                    refresh_trigger=getattr(self, "refresh_form_trigger", None),
                    get_settings=getattr(self, "get_current_model_settings", None),
                    title="📤 Send frame to")
            except Exception:
                traceback.print_exc()  # SendTo not installed -> no frame panel

            # -- send edited CLIP: host "Continue Video" + path-payload plugins
            # The whole edited clip can feed the Media Generator's Continue-Video
            # (video_source) source AND/OR any path-payload SendTo receiver (e.g.
            # Reel2Reel). Decoupled — no plugin class is imported; the host target
            # needs only get_current_model_settings, not SendTo.
            clip_targets = []
            if callable(getattr(self, "get_current_model_settings", None)):
                clip_targets.append(
                    {"label": "▶ Continue Video (Media Generator)", "kind": "continue"})
            try:
                from sendto.targets import available_targets, enqueue as _enqueue
                self._sendto_enqueue = _enqueue
                for t in available_targets(include_host=False):
                    if t.get("payload") == "path" and t.get("tab") != PLUGIN_ID:
                        clip_targets.append(
                            {"label": t["label"], "kind": "plugin",
                             "tab": t.get("tab"), "inbox_key": t.get("inbox_key")})
            except Exception:
                self._sendto_enqueue = None  # SendTo absent -> host targets only

            main_tabs = getattr(self, "main_tabs", None)
            if clip_targets and main_tabs is not None:
                self._clip_targets_by_label = {t["label"]: t for t in clip_targets}
                labels = [t["label"] for t in clip_targets]
                with gr.Accordion("🎬 Send edited clip to", open=False):
                    with gr.Row():
                        clip_target = gr.Dropdown(labels, value=labels[0],
                                                  label="Send clip to", scale=2)
                        clip_send_btn = gr.Button("🎬 Send edited clip →",
                                                  variant="primary", scale=1)
                trig = getattr(self, "refresh_form_trigger", None)
                has_trig = trig is not None
                outs_clip = [main_tabs] + ([trig] if has_trig else [])

                def _clip_send_evt(label, state_val):
                    nav, stamp = self._send_clip(label, state_val)
                    return [nav, stamp] if has_trig else [nav]

                clip_send_btn.click(_clip_send_evt,
                                    inputs=[clip_target, self.state],
                                    outputs=outs_clip)

            self._wire(c)

        # On tab entry: drain the inbox and refresh the whole editor view. The
        # host renders the tab body by side-effect (it ignores this return), so
        # what matters is that on_tab_outputs aligns 1:1 with on_tab_select.
        self.on_tab_outputs = self._refresh_outs()

    # ----------------------------------------------------------------------
    #  refresh contract — kept in lockstep across _ingest / edits / history /
    #  on_tab_select. Order matters: every producer returns exactly this many
    #  updates, in this order.
    # ----------------------------------------------------------------------
    def _refresh_outs(self) -> list:
        c = self._c
        return ([c["preview"], c["info_md"], c["filmstrip"]]
                + self._trim_comps()
                + [c["playhead"], c["start_thumb"], c["end_thumb"], c["time_md"],
                   c["status_md"], c["undo_btn"], c["redo_btn"], c["reset_btn"],
                   c["save_inplace_btn"], c["save_copy_btn"]])

    def _refresh(self, status: str = "") -> list:
        """Build the full refresh update list from the current clip state."""
        cur = self._clip.current
        if cur is None:
            return ([gr.update(value=None), gr.update(value=""),
                     gr.update(value=None)]
                    + self._range_update(0, 1, 1)
                    + [gr.update(minimum=0, maximum=1, value=0),
                       gr.update(value=None), gr.update(value=None),
                       gr.update(value=""), gr.update(value=status),
                       gr.update(interactive=False), gr.update(interactive=False),
                       gr.update(interactive=False), gr.update(interactive=False),
                       gr.update(interactive=False)])

        maxv = max(int(cur.num_frames) - 1, 0)
        strip = filmstrip.build_filmstrip(
            cur.path, get_video_frame=self._gvf())
        has_origin = bool(cur.origin) and not cur.is_upload
        return ([gr.update(value=cur.path),
                 gr.update(value=_fmt_info(cur)),
                 gr.update(value=strip)]
                + self._range_update(0, maxv, maxv)
                + [gr.update(minimum=0, maximum=maxv, value=0),
                   gr.update(value=self._safe_frame(cur.path, 0)),
                   gr.update(value=self._safe_frame(cur.path, maxv)),
                   gr.update(value=_fmt_time(0, maxv, cur.fps)),
                   gr.update(value=status),
                   gr.update(interactive=self._clip.can_undo()),
                   gr.update(interactive=self._clip.can_redo()),
                   gr.update(interactive=True),
                   gr.update(interactive=has_origin),
                   gr.update(interactive=True)])

    def _gvf(self):
        """The host's get_video_frame (for the PIL filmstrip fallback), or None."""
        try:
            from shared.utils.utils import get_video_frame
            return get_video_frame
        except Exception:
            return None

    # -- clip ingest --------------------------------------------------------
    def _probe_to_info(self, path: str, is_upload: bool) -> "clipstate.ClipInfo":
        info = ffmpeg.probe(path, self._gvi())
        return clipstate.ClipInfo(
            path=path,
            fps=float(info.get("fps") or 0.0),
            num_frames=int(info.get("num_frames") or 0),
            duration=float(info.get("duration") or 0.0),
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            has_audio=bool(info.get("has_audio")),
            fps_inexact=bool(info.get("_fps_inexact")),
            is_upload=bool(is_upload),
            origin=(None if is_upload else path))

    def _ingest(self, path, is_upload: bool) -> list:
        """Load *path* as a fresh clip (clears history) and return a full refresh."""
        path = self._resolve(path)
        if not path or not str(path).lower().endswith(_VIDEO_EXTS):
            gr.Warning("That selection isn't a video file.")
            return self._refresh("⚠️ Not a video.")
        try:
            info = self._probe_to_info(path, is_upload)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("Couldn't read that video.")
            return self._refresh(f"⚠️ Could not read the video: {exc}")
        if info.fps <= 0 or info.num_frames <= 0:
            gr.Warning("Couldn't detect FPS / frame count for that video.")
            return self._refresh("⚠️ No FPS/frames detected.")
        self._clip.load(info)
        return self._refresh("Loaded. Edit, then Save in place or as a copy.")

    def _push_edited(self, new_path: str, status: str) -> list:
        """Probe a freshly rendered working file, push it, and refresh.

        ``push`` carries origin/is_upload forward, so re-probe needn't set them."""
        try:
            info = self._probe_to_info(new_path, is_upload=False)
        except Exception:
            traceback.print_exc()
            # The edit succeeded but re-probe failed: fall back to the prior fps.
            prev = self._clip.current
            info = clipstate.ClipInfo(
                path=new_path,
                fps=(prev.fps if prev else 0.0),
                num_frames=(prev.num_frames if prev else 0),
                duration=(prev.duration if prev else 0.0),
                width=(prev.width if prev else 0),
                height=(prev.height if prev else 0),
                has_audio=(prev.has_audio if prev else False),
                fps_inexact=True)
        self._clip.push(info)
        return self._refresh(status)

    # -- wiring -------------------------------------------------------------
    def _wire(self, c):
        outs = self._refresh_outs()
        trim_comps = self._trim_comps()

        # ---- load ---------------------------------------------------------
        gallery_tab_comp = getattr(self, "current_gallery_tab", None)
        if gallery_tab_comp is not None:
            c["load_btn"].click(self._load_from_preview,
                                inputs=[self.state, gallery_tab_comp], outputs=outs)
        else:
            c["load_btn"].click(lambda st: self._load_from_preview(st, 0),
                                inputs=[self.state], outputs=outs)
        c["upload_video"].upload(self._load_from_upload,
                                 inputs=[c["upload_video"]], outputs=outs)

        # ---- mini-timeline: range release -> time + in/out thumbs ---------
        range_out = [c["time_md"], c["start_thumb"], c["end_thumb"]]
        for comp in trim_comps:
            comp.release(self._on_range, inputs=trim_comps, outputs=range_out)

        # ---- trim & split -------------------------------------------------
        c["trim_btn"].click(self._on_trim, inputs=trim_comps, outputs=outs)
        c["split_btn"].click(self._on_split,
                             inputs=[c["playhead"], c["keep_radio"]], outputs=outs)

        # ---- transform ----------------------------------------------------
        c["flip_h_btn"].click(lambda: self._on_flip(True, False), outputs=outs)
        c["flip_v_btn"].click(lambda: self._on_flip(False, True), outputs=outs)

        # ---- resize -------------------------------------------------------
        c["resize_btn"].click(
            self._on_resize,
            inputs=[c["rs_w"], c["rs_h"], c["rs_lock"], c["rs_presets"]],
            outputs=outs)

        # ---- crop ---------------------------------------------------------
        c["crop_btn"].click(
            self._on_crop,
            inputs=[c["crop_x"], c["crop_y"], c["crop_w"], c["crop_h"]],
            outputs=outs)

        # ---- speed --------------------------------------------------------
        c["speed_btn"].click(self._on_speed, inputs=[c["speed"]], outputs=outs)

        # ---- colour -------------------------------------------------------
        c["color_btn"].click(
            self._on_color,
            inputs=[c["bri"], c["con"], c["sat"], c["hue"], c["warmth"],
                    c["gamma"]],
            outputs=outs)
        c["color_reset_btn"].click(
            lambda: (gr.update(value=100), gr.update(value=100),
                     gr.update(value=100), gr.update(value=0),
                     gr.update(value=0), gr.update(value=1.0)),
            outputs=[c["bri"], c["con"], c["sat"], c["hue"], c["warmth"],
                     c["gamma"]])

        # ---- history ------------------------------------------------------
        c["undo_btn"].click(self._on_undo, outputs=outs)
        c["redo_btn"].click(self._on_redo, outputs=outs)
        c["reset_btn"].click(self._on_reset, outputs=outs)

        # ---- save ---------------------------------------------------------
        c["save_inplace_btn"].click(self._save_inplace, outputs=[c["status_md"]])
        c["save_copy_btn"].click(self._save_copy, inputs=[c["save_name"]],
                                 outputs=[c["status_md"]])

    # -- load handlers ------------------------------------------------------
    def _load_from_preview(self, state, gallery_tab):
        gen = (state or {}).get("gen", {}) or {}
        if gallery_tab == 1 or gen.get("current_gallery_source") == "audio":
            gr.Warning("That's an audio selection — pick a video clip.")
            return self._refresh("⚠️ Audio tab selected.")
        path = self._current_selection_path(state)
        if not path:
            gr.Warning("Select a result in the gallery above first.")
            return self._refresh("⚠️ Nothing selected in the gallery.")
        return self._ingest(path, is_upload=False)

    def _load_from_upload(self, path):
        if not path:
            return self._refresh("")
        return self._ingest(path, is_upload=True)

    # -- mini-timeline ------------------------------------------------------
    def _on_range(self, *range_args):
        cur = self._clip.current
        if cur is None:
            return gr.update(), gr.update(), gr.update()
        lo, hi = self._read_range(range_args)
        return (gr.update(value=_fmt_time(lo, hi, cur.fps)),
                gr.update(value=self._safe_frame(cur.path, lo)),
                gr.update(value=self._safe_frame(cur.path, hi)))

    # -- edit handlers ------------------------------------------------------
    def _on_trim(self, *range_args):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        if cur.fps <= 0:
            gr.Warning("Unknown FPS — can't compute a frame-accurate cut.")
            return self._refresh("⚠️ Unknown FPS.")
        if cur.fps_inexact:
            gr.Warning("Exact FPS unavailable (ffprobe failed); frame timing is "
                       "approximate — the last frame may be off by one.")
        lo, hi = self._read_range(range_args)
        start_sec = trim.frame_to_sec(lo, cur.fps)
        end_sec = min(trim.end_frame_to_sec(hi, cur.fps),
                      cur.duration or trim.end_frame_to_sec(hi, cur.fps))
        if end_sec <= start_sec:
            gr.Warning("Select at least one frame.")
            return self._refresh("⚠️ Empty range.")
        try:
            dst = paths.work_path(".mp4")
            trim.trim_video_precise(cur.path, dst, start_sec, end_sec)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to trim — see the console.")
            return self._refresh(f"❌ Trim failed: {exc}")
        gr.Info("Trimmed.")
        return self._push_edited(
            dst, f"✅ Trimmed to frames {lo}–{hi} "
                 f"({max(end_sec - start_sec, 0):.3f}s).")

    def _on_split(self, playhead, keep_radio):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        if cur.fps <= 0:
            gr.Warning("Unknown FPS — can't split frame-accurately.")
            return self._refresh("⚠️ Unknown FPS.")
        frame = int(round(playhead or 0))
        last = max(int(cur.num_frames) - 1, 0)
        if frame <= 0 or frame >= last:
            gr.Warning("Move the playhead inside the clip to split.")
            return self._refresh("⚠️ Playhead must be inside the clip.")
        keep_tail = (keep_radio == "Keep tail")
        try:
            head_dst = paths.work_path(".mp4")
            tail_dst = paths.work_path(".mp4")
            trim.split_at_frame(cur.path, head_dst, tail_dst, frame, cur.fps)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to split — see the console.")
            return self._refresh(f"❌ Split failed: {exc}")
        kept = tail_dst if keep_tail else head_dst
        which = "tail" if keep_tail else "head"
        gr.Info(f"Split at frame {frame} — kept the {which}.")
        return self._push_edited(kept, f"✅ Split at frame {frame}, kept the {which}.")

    def _on_flip(self, horizontal: bool, vertical: bool):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        try:
            dst = ops.apply_flip(cur.path, horizontal, vertical)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to flip — see the console.")
            return self._refresh(f"❌ Flip failed: {exc}")
        axis = "horizontally" if horizontal else "vertically"
        gr.Info(f"Flipped {axis}.")
        return self._push_edited(dst, f"✅ Flipped {axis}.")

    def _on_resize(self, rs_w, rs_h, rs_lock, preset):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        width, height = self._resolve_resize(rs_w, rs_h, rs_lock, preset)
        if width is None and height is None:
            gr.Warning("Set a width and/or height (or pick a preset).")
            return self._refresh("⚠️ No resize dimensions given.")
        try:
            dst = ops.apply_resize(cur.path, width, height)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to resize — see the console.")
            return self._refresh(f"❌ Resize failed: {exc}")
        gr.Info("Resized.")
        return self._push_edited(
            dst, f"✅ Resized to {width or 'auto'}×{height or 'auto'}.")

    @staticmethod
    def _resolve_resize(rs_w, rs_h, rs_lock, preset):
        """Resolve the requested (width, height); either may be None for auto.

        A non-"Keep" preset wins; otherwise the typed numbers apply, and when
        *rs_lock* is set a blank dimension is left None so ffmpeg auto-fits it
        (``-2``) to preserve aspect. ``0``/blank are treated as unset."""
        if preset and preset != "Keep" and "×" in preset:
            try:
                pw, ph = preset.split("×")
                return int(pw), int(ph)
            except Exception:
                pass

        def _num(v):
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                return None
            return n if n > 0 else None

        w = _num(rs_w)
        h = _num(rs_h)
        if rs_lock:
            # Lock aspect: keep at most one explicit dim so the other auto-fits.
            if w and h:
                h = None  # width drives; height auto-fits to preserve aspect
        return w, h

    def _on_crop(self, x, y, w, h):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")

        def _i(v, default=0):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return default

        cx, cy, cw, ch = _i(x), _i(y), _i(w), _i(h)
        if cw <= 0 or ch <= 0:
            gr.Warning("Crop width and height must be positive.")
            return self._refresh("⚠️ Invalid crop size.")
        if (cx + cw) > cur.width or (cy + ch) > cur.height:
            gr.Warning("Crop rectangle extends past the frame.")
            return self._refresh("⚠️ Crop exceeds frame bounds.")
        try:
            dst = ops.apply_crop(cur.path, cx, cy, cw, ch)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to crop — see the console.")
            return self._refresh(f"❌ Crop failed: {exc}")
        gr.Info("Cropped.")
        return self._push_edited(dst, f"✅ Cropped to {cw}×{ch} at ({cx}, {cy}).")

    def _on_speed(self, speed):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        try:
            factor = float(speed or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        if factor <= 0:
            gr.Warning("Speed must be greater than zero.")
            return self._refresh("⚠️ Invalid speed.")
        if abs(factor - 1.0) < 1e-6:
            gr.Warning("Speed is 1× — nothing to change.")
            return self._refresh("⚠️ Speed is 1×.")
        try:
            dst = ops.apply_speed(cur.path, factor)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to change speed — see the console.")
            return self._refresh(f"❌ Speed change failed: {exc}")
        gr.Info(f"Speed set to {factor:g}×.")
        return self._push_edited(dst, f"✅ Speed set to {factor:g}×.")

    def _on_color(self, bri, con, sat, hue, warmth, gamma):
        cur = self._clip.current
        if cur is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        # Map UI slider ranges to ffmpeg eq/hue/warmth parameters.
        b = (float(bri) - 100.0) / 100.0
        c = float(con) / 100.0
        s = float(sat) / 100.0
        g = float(gamma)
        h = float(hue)
        warm = float(warmth)
        if not ops.color_vf(b, c, s, g, h, warm):
            gr.Warning("All colour sliders are neutral — nothing to apply.")
            return self._refresh("⚠️ Colour controls are neutral.")
        try:
            dst = ops.apply_color(cur.path, b, c, s, g, h, warm)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to colour-correct — see the console.")
            return self._refresh(f"❌ Colour grade failed: {exc}")
        gr.Info("Colour grade applied.")
        return self._push_edited(dst, "✅ Colour grade applied.")

    # -- history ------------------------------------------------------------
    def _on_undo(self):
        if not self._clip.can_undo():
            gr.Warning("Nothing to undo.")
            return self._refresh("")
        self._clip.undo()
        gr.Info("Undone.")
        return self._refresh("↶ Undone.")

    def _on_redo(self):
        if not self._clip.can_redo():
            gr.Warning("Nothing to redo.")
            return self._refresh("")
        self._clip.redo()
        gr.Info("Redone.")
        return self._refresh("↷ Redone.")

    def _on_reset(self):
        if self._clip.current is None:
            gr.Warning("Load a video first.")
            return self._refresh("⚠️ Load a clip first.")
        self._clip.reset()
        gr.Info("Reset to the originally loaded clip.")
        return self._refresh("⟲ Reset to the original.")

    # -- save ---------------------------------------------------------------
    def _save_inplace(self):
        cur = self._clip.current
        if cur is None or not cur.path or not os.path.exists(cur.path):
            gr.Warning("Nothing to save yet — load and edit a clip first.")
            return "⚠️ Load and edit a clip first."
        if cur.is_upload or not cur.origin:
            try:
                dest = paths.save_as_copy(cur.path, cur.origin, "", self._save_path())
            except Exception as exc:
                traceback.print_exc()
                gr.Warning("Save failed — see the console.")
                return f"❌ Save failed: {exc}"
            gr.Info("Uploaded source has no original to overwrite — saved a copy.")
            return (f"📑 Uploaded source: saved a copy → `{dest}` "
                    "(appears in the gallery after the next refresh).")
        try:
            paths.save_in_place(cur.path, cur.origin)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("Save failed — see the console.")
            return f"❌ Save failed: {exc}"
        gr.Info("Saved — overwrote the original.")
        return (f"💾 Overwrote original → `{cur.origin}`. The gallery thumbnail "
                "refreshes on the next reselect/regenerate.")

    def _save_copy(self, name):
        cur = self._clip.current
        if cur is None or not cur.path or not os.path.exists(cur.path):
            gr.Warning("Nothing to save yet — load and edit a clip first.")
            return "⚠️ Load and edit a clip first."
        try:
            dest = paths.save_as_copy(cur.path, cur.origin, name, self._save_path())
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("Save failed — see the console.")
            return f"❌ Save failed: {exc}"
        gr.Info("Saved a copy to the outputs folder.")
        return (f"📑 Saved copy → `{dest}` "
                "(appears in the gallery after the next refresh).")

    # -- send edited clip ---------------------------------------------------
    def _send_clip(self, label, state_val):
        """Hand the whole edited clip to the Media Generator's Continue-Video
        (``video_source``) source, or to a path-payload SendTo receiver (e.g.
        Reel2Reel). Returns ``(main_tabs_update, form_trigger_stamp)``."""
        noop = gr.update()
        cur = self._clip.current
        if cur is None or not cur.path:
            gr.Warning("Load and edit a clip first.")
            return noop, noop
        spec = (self._clip_targets_by_label or {}).get(label)
        if spec is None:
            gr.Warning("Unknown target.")
            return noop, noop

        if spec.get("kind") == "continue":
            # Continue Video: set the generator's video_source + ensure the
            # video-continuation flag ("V" in image_prompt_type), then navigate
            # to the generator and poke the form trigger (Reel2Reel's pattern).
            try:
                s = self.get_current_model_settings(state_val)
                s["video_source"] = cur.path
                ipt = s.get("image_prompt_type") or ""
                if "V" not in ipt:
                    s["image_prompt_type"] = ("V" + ipt) if ipt else "V"
            except Exception:
                traceback.print_exc()
                gr.Warning("Could not push the clip to the Media Generator.")
                return noop, noop
            gr.Info("Sent the edited clip to the Media Generator as the "
                    "Continue-Video source. Pick a video-continuation–capable "
                    "model there if the current one isn't one.")
            return gr.update(selected="media_gen"), time.time()

        # path-payload plugin receiver (e.g. Reel2Reel) via the SendTo contract
        if not callable(getattr(self, "_sendto_enqueue", None)):
            gr.Warning("SendTo isn't installed — can't hand off to that plugin.")
            return noop, noop
        try:
            self._sendto_enqueue(state_val, spec.get("inbox_key"), cur.path,
                                 payload="path")
        except Exception:
            traceback.print_exc()
            gr.Warning("Could not hand the clip over.")
            return noop, noop
        gr.Info(f"Sent the edited clip to {label} (loads when you open the tab).")
        tab = spec.get("tab")
        return (gr.update(selected=f"plugin_{tab}") if tab else noop), noop

    # -- inbox --------------------------------------------------------------
    def on_tab_select(self, state: dict):
        try:
            items = inbox.drain(state)
            if items:
                # Ingest the most recent hand-off as a non-upload clip.
                return self._ingest(items[-1], is_upload=False)
        except Exception:
            traceback.print_exc()
        # Nothing queued: no-op updates aligned 1:1 with on_tab_outputs.
        return [gr.update() for _ in self.on_tab_outputs]
