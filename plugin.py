"""Mutator — a Wan2GP plugin (v0.2: per-clip single-track editor).

One main-webui tab laid out as three vertical zones — WORKSPACE (a source
``gr.Video`` + a compact tool row + a draggable single-track timeline of
Segments + a togglable crop canvas) → RESULT (a ``gr.Video`` of the SELECTED
segment's render + info) → SEND (save in place / save as copy + an embedded
SendTo frame panel + "Send edited clip"). The timeline is ONE ordered track of
:class:`core.model.Segment`\\ s (it starts as one segment spanning the whole
source). Selecting a segment loads ITS OWN edits into the tools + Result. Each
segment carries independent trim / crop / resize / flip / speed / reverse /
colour. SPLICE razors the selected segment at the playhead into two halves that
inherit every edit; REJOIN merges adjacent same-source contiguous segments.

It composites/transforms *existing* clips with ffmpeg only — it never generates
frames, so it needs none of the host's submit_task / model machinery. Editor
state (the :class:`~core.model.Track` + its undo/redo) lives on this single
per-process plugin instance (``self._track``), like the sibling plugins; the
per-session ``state`` dict carries only the SendTo inbox hand-off. Single-user /
local use.

Two independent JS bridges wire the browser to Python: the timeline ships its JS
via ``add_custom_js`` and round-trips edit-JSON through ``mut_tl_to_py`` (JS→Py,
debounced) and ``mut_tl_from_py`` (Py→JS op-envelope, monotonic ``seq``); the
crop canvas is a self-contained ``<iframe srcdoc>`` writing ``{seg_id,x,y,w,h}``
source-pixel coords into ``mut_crop_to_py`` and receiving a source frame via the
one-shot injector ``mut_crop_from_py``.

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

from .core import ffmpeg, inbox, paths, render
from .core.model import COLOUR_NEUTRAL, Track
from .ui import crop, logo, styles, suite
from .ui import timeline as tw

PLUGIN_ID = "Mutator"
PLUGIN_NAME = "Mutator"

# The selection-driven tool-row outputs, in the fixed order suite.TOOL_OUT_KEYS
# defines: rs_w, rs_h, rs_lock, rs_aspect, speed, reverse_chk, then the six
# colour sliders. _selection_values(seg) returns updates 1:1 with this list.
# Built per-instance in create_ui from the component dict (see self._tool_outs).


class Mutator(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PLUGIN_NAME
        # Single source of truth for the version: plugin_info.json (fallback 0.2.0).
        try:
            self.version = json.loads(
                (Path(__file__).parent / "plugin_info.json").read_text()
            ).get("version", "0.2.0")
        except Exception:
            self.version = "0.2.0"
        self.description = (
            "Per-clip single-track video editor: load a clip (gallery / upload / "
            "SendTo), build an ordered timeline of segments, then per-segment trim, "
            "splice, rejoin, crop, resize, flip, speed, reverse and colour — with "
            "undo/redo. Save in place or as a copy; send a frame or the edited clip "
            "onward via SendTo or Continue-Video."
        )
        # The single ordered edit track + its undo/redo history. Held on the single
        # per-process instance (single-user/local), mirroring Reel2Reel's
        # self._project. The per-session `state` dict carries only the inbox.
        self._track = Track()
        self._seq = 0
        self._last_sig = self._track.content_sig()
        # Per-source duration ceiling for trims (source path -> probed duration).
        self._src_dur: dict[str, float] = {}
        self._c: dict = {}
        self._tool_outs: list = []
        # "Send edited clip" target registry + SendTo enqueue fn (set in create_ui).
        self._clip_targets_by_label: dict = {}
        self._sendto_enqueue = None
        self._api = None
        # Whether the crop panel is currently open (gr.update(visible=…) does not
        # mutate the component object, so track the toggle state here).
        self._crop_open = False

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

    # -- relax Gradio file access (carried from v0.1 / Reel2Reel) ----------
    def _relax_file_access(self):
        """Additively let Gradio accept event-input files that live under
        Mutator's own dirs (renders / thumbs / cache / outputs). The sibling
        plugins monkeypatch gradio's ``check_all_files_in_cache`` against THEIR
        own allow-list, so our renders would otherwise be rejected with
        'File ... is not accessible' when fed back IN to the Result player or a
        SendTo hand-off. We wrap whatever check is in place: on a rejection we
        allow our files and re-raise for anything foreign. Purely additive (never
        stricter), load-order independent, fully guarded — never breaks the app."""
        try:
            import tempfile

            import gradio.processing_utils as _pu
            import gradio_client.utils as _cu
        except Exception:
            return
        if getattr(_pu, "_mutator_cache_patch", False):
            return
        prev = getattr(_pu, "check_all_files_in_cache", None)
        if not callable(prev):
            return

        def _allow_dirs():
            cand = []
            try:
                from gradio.context import Context
                cand.append(getattr(Context.root_block, "GRADIO_CACHE", None))
            except Exception:
                pass
            try:
                from gradio.utils import get_upload_folder
                cand.append(get_upload_folder())
            except Exception:
                pass
            cand += [
                os.environ.get("GRADIO_TEMP_DIR"),
                os.path.join(tempfile.gettempdir(), "gradio"),
                os.path.join(os.getcwd(), "outputs"),
            ]
            for fn in (paths.cache_dir, paths.renders_dir, paths.thumbs_dir):
                try:
                    cand.append(fn())
                except Exception:
                    pass
            try:
                cand.append(paths.outputs_dir(self._save_path()))
            except Exception:
                pass
            out = []
            for p in cand:
                try:
                    if p:
                        out.append(os.path.realpath(str(p)))
                except Exception:
                    pass
            return out

        def _under(p, bases):
            try:
                rp = os.path.realpath(p)
            except Exception:
                return False
            for b in bases:
                try:
                    if os.path.commonpath([rp, b]) == b:
                        return True
                except (ValueError, OSError):
                    continue
            return False

        def _lenient(data):
            try:
                prev(data)
            except (ValueError, gr.Error) as e:
                bases = _allow_dirs()

                def _ok(d):
                    p = d.get("path", "") if isinstance(d, dict) else ""
                    if not p or _cu.is_http_url_like(p):
                        return
                    if os.path.exists(p) and _under(p, bases):
                        return
                    raise e
                _cu.traverse(data, _ok, _cu.is_file_obj)

        _pu.check_all_files_in_cache = _lenient
        _pu._mutator_cache_patch = True

    # -- lifecycle ----------------------------------------------------------
    def setup_ui(self):
        # 1. Reclaim stale working/render/thumb files from earlier sessions.
        try:
            paths.prune_cache()
        except Exception:
            traceback.print_exc()

        # 2. Ship the timeline JS via add_custom_js (the ONLY path that runs).
        #    crop.js ships INSIDE the crop iframe srcdoc, NOT here.
        try:
            js = "\n".join(filter(None, [tw.timeline_js()]))
            if js:
                self.add_custom_js(js)
        except Exception:
            traceback.print_exc()

        # 3. Let Gradio serve our assets + render/thumb/cache/outputs dirs OUT.
        try:
            tw.register_static_paths([
                paths.cache_dir(), paths.renders_dir(), paths.thumbs_dir(),
                paths.outputs_dir(self._save_path()),
            ])
        except Exception:
            traceback.print_exc()

        # 4. ...and accept those same files back IN as event inputs.
        self._relax_file_access()

        # 5. SendTo "path" sender contract alias.
        self._register_inbox_alias()

        # 6. Host components + globals.
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

    # -- UI -----------------------------------------------------------------
    def create_ui(self, api_session=None):
        # api_session: zero-arg on the local host, a session on the newer API
        # host. Mutator is ffmpeg-only, so it is unused beyond being stored.
        self._api = api_session

        with gr.Column(elem_id="mutator-root"):
            # -- tab accent + tagger + logo banner (banner at the very top) ----
            # A <style> blob plus a tiny JS tagger that finds the tab button by
            # its label and adds the .mutator-tabbtn cyan outline, then the logo
            # banner (sized/positioned like Image Suite's).
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

            # The whole tab body (WORKSPACE / RESULT / SEND). build_ui spreads
            # the timeline + crop widget dicts into c.
            c = suite.build_ui()
            self._c = c

            # -- embedded SendTo: FRAME send panel (best-effort) -------------
            # Sends the still at the selected segment's playhead frame to any
            # "image" target. Absent SendTo just omits the panel.
            self._build_frame_panel(c)

            # -- "Send edited clip" target list ------------------------------
            self._build_clip_targets(c)

            # -- store component handles on self -----------------------------
            self.tl_to_py = c["tl_to_py"]
            self.tl_from_py = c["tl_from_py"]
            self.crop_to_py = c["crop_to_py"]
            self.crop_from_py = c["crop_from_py"]
            self.crop_panel = c["crop_panel"]
            self.src_video = c["src_video"]
            self.result_video = c["result_video"]
            self.result_info = c["result_info"]
            self.status_md = c["status_md"]
            self.save_name = c["save_name"]
            self.save_inplace_btn = c["save_inplace_btn"]
            self.save_copy_btn = c["save_copy_btn"]
            self.send_target = c["send_target"]
            self.send_clip_btn = c["send_clip_btn"]
            self.undo_btn = c["undo_btn"]
            self.redo_btn = c["redo_btn"]
            # The selection-driven tool row, 1:1 with suite.TOOL_OUT_KEYS.
            self._tool_outs = [c[k] for k in suite.TOOL_OUT_KEYS]

            # Fresh track baseline.
            self._track = Track()
            self._last_sig = self._track.content_sig()

            self._wire(c)

        # ---------------------------------------------------------------- #
        #  on_tab_outputs — the refresh contract. EVERY handler that targets   #
        #  these must return values in this exact order (or a documented        #
        #  prefix of it). on_tab_select aligns 1:1 with this full list.         #
        #                                                                       #
        #  [ 0] tl_from_py        (op-envelope -> applyOp in the browser)        #
        #  [ 1] result_video      (selected segment render path)                #
        #  [ 2] result_info       (info markdown)                               #
        #  [ 3] src_video         (the loaded source player)                    #
        #  [ 4..15] *TOOL_OUTS (12): rs_w, rs_h, rs_lock, rs_aspect, speed,      #
        #           reverse_chk, col_bri, col_con, col_sat, col_hue, col_warm,   #
        #           col_gamma                                                    #
        #  [16] undo_btn          (interactive update)                          #
        #  [17] redo_btn          (interactive update)                          #
        #  [18] save_inplace_btn  (interactive update)                          #
        #  [19] save_copy_btn     (interactive update)                          #
        # ---------------------------------------------------------------- #
        self.on_tab_outputs = self._refresh_outs()
        return c

    def _refresh_outs(self) -> list:
        return ([self.tl_from_py, self.result_video, self.result_info,
                 self.src_video]
                + list(self._tool_outs)
                + [self.undo_btn, self.redo_btn,
                   self.save_inplace_btn, self.save_copy_btn])

    # -- embedded SendTo panels (create_ui helpers) -------------------------
    def _build_frame_panel(self, c):
        """Build the embedded SendTo FRAME panel inside c['send_frame_slot'].

        Sends the source frame at the selected segment's playhead. Best-effort:
        absent SendTo just leaves the slot empty."""
        try:
            from sendto.embed import build_send_panel

            def _frame_at(_unused=None):
                sel = self._track.selected()
                if sel is None:
                    return None
                # The playhead is a TIMELINE second; convert to a source second
                # within the selected clip, then extract that raw source frame.
                at_src = self._playhead_src_sec(sel)
                uri = render.extract_frame(sel, at_src)
                return uri or None

            with c["send_frame_slot"]:
                build_send_panel(
                    state=self.state, main_tabs=getattr(self, "main_tabs", None),
                    image_inputs=[self.tl_to_py],
                    to_path=_frame_at,
                    refresh_trigger=getattr(self, "refresh_form_trigger", None),
                    get_settings=getattr(self, "get_current_model_settings", None),
                    title="📤 Send frame to")
        except Exception:
            traceback.print_exc()  # SendTo not installed -> no frame panel

    def _build_clip_targets(self, c):
        """Populate the "Send edited clip" dropdown: Continue Video + any
        path-payload SendTo receivers. Decoupled — no plugin class is imported;
        the host "Continue Video" target needs only get_current_model_settings."""
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
            self._sendto_enqueue = None  # SendTo absent -> host target only

        self._clip_targets_by_label = {t["label"]: t for t in clip_targets}
        labels = [t["label"] for t in clip_targets]
        value = labels[0] if labels else None
        try:
            c["send_target"].choices = labels
            c["send_target"].value = value
        except Exception:
            pass

    # -- envelopes / signatures (§7.4) --------------------------------------
    def _refresh_thumbs(self):
        """Regenerate any stale per-segment filmstrip (sig-keyed; a cache hit is
        free). to_json then emits seg.thumb_path as thumb_url."""
        for s in self._track.segments:
            if not s.source:
                continue
            sig = render.segment_render_sig(s)
            dest = paths.cached_thumb_path(sig)
            if s.thumb_path == dest and os.path.exists(dest):
                continue
            if os.path.exists(dest):
                s.thumb_path = dest
                continue
            try:
                s.thumb_path = render.filmstrip_for(s) or None
            except Exception:
                s.thumb_path = None

    def _edit_payload(self) -> dict:
        self._refresh_thumbs()
        edit = self._track.to_json()
        for s in edit["segments"]:
            rp = s.get("render_path")
            tp = s.get("thumb_url")
            s["render_path"] = tw.file_url(rp) if rp else None
            s["thumb_url"] = tw.file_url(tp) if tp else None
        return edit

    def _load_envelope(self) -> str:
        self._seq += 1
        return json.dumps({"seq": self._seq, "op": "load",
                           "edit": self._edit_payload()})

    def _env_after(self) -> str:
        """After a server-side mutation: rebaseline the undo signature + envelope."""
        self._last_sig = self._track.content_sig()
        return self._load_envelope()

    def _render_selected(self):
        """Render the selected segment (cache-first) and return its abs path, or
        None. Stores seg.render_path/render_sig. ffmpeg failures surface a
        Warning and return None."""
        sel = self._track.selected()
        if sel is None:
            return None
        try:
            path = render.render_segment(sel, has_audio=self._track.has_audio)
        except ffmpeg.FFmpegError as exc:
            traceback.print_exc()
            gr.Warning("ffmpeg failed to render that clip — see the console.")
            _ = exc
            return None
        except Exception:
            traceback.print_exc()
            gr.Warning("Could not render that clip — see the console.")
            return None
        sel.render_path = path
        sel.render_sig = render.segment_render_sig(sel)
        return path

    # -- info / selection readouts ------------------------------------------
    def _seg_start_sec(self, seg) -> float:
        """The selected segment's computed TIMELINE start (running sum of prior
        segment durations)."""
        running = 0.0
        for s in self._track.segments:
            if s.id == seg.id:
                return running
            running += s.dur
        return 0.0

    def _playhead_src_sec(self, seg) -> float:
        """Convert the track playhead (a TIMELINE second) to a SOURCE second
        within ``seg``: in_ + (playhead - seg_start) * speed, clamped to the
        segment's source window."""
        start = self._seg_start_sec(seg)
        at = float(seg.in_) + max(0.0, float(self._track.playhead) - start) * seg.speed_f
        lo, hi = float(seg.in_), float(seg.out)
        return max(lo, min(hi, at))

    def _result_info(self, seg) -> str:
        if seg is None:
            return ""
        w = seg.src_w or 0
        h = seg.src_h or 0
        if seg.crop:
            w, h = seg.crop["w"], seg.crop["h"]
        if seg.resize:
            w = seg.resize.get("w") or w
            h = seg.resize.get("h") or h
        flags = []
        if seg.crop:
            flags.append("crop")
        if seg.resize:
            flags.append("resize")
        if seg.flip_h:
            flags.append("flip-h")
        if seg.flip_v:
            flags.append("flip-v")
        if abs(seg.speed_f - 1.0) > 1e-6:
            flags.append(f"{seg.speed_f:g}×")
        if seg.reverse:
            flags.append("reverse")
        if not seg.is_neutral_colour:
            flags.append("graded")
        edits = (" · " + ", ".join(flags)) if flags else " · (no edits)"
        audio = "🔊 audio" if self._track.has_audio else "🔇 no audio"
        return (f"**{seg.label or 'Clip'}**  ·  {w}×{h}  ·  "
                f"**{seg.dur:.2f}s** ({seg.src_len:.2f}s src)  ·  {audio}{edits}")

    def _selection_values(self, seg) -> list:
        """Tool-row updates 1:1 with suite.TOOL_OUT_KEYS (rs_w, rs_h, rs_lock,
        rs_aspect, speed, reverse_chk, col_bri..col_gamma). Neutral defaults when
        nothing is selected."""
        if seg is None:
            return [
                gr.update(value=None), gr.update(value=None),
                gr.update(value=True), gr.update(value="free"),
                gr.update(value=1.0), gr.update(value=False),
                gr.update(value=COLOUR_NEUTRAL["brightness"]),
                gr.update(value=COLOUR_NEUTRAL["contrast"]),
                gr.update(value=COLOUR_NEUTRAL["saturation"]),
                gr.update(value=COLOUR_NEUTRAL["hue"]),
                gr.update(value=COLOUR_NEUTRAL["warmth"]),
                gr.update(value=COLOUR_NEUTRAL["gamma"]),
            ]
        rw = seg.resize.get("w") if seg.resize else None
        rh = seg.resize.get("h") if seg.resize else None
        col = seg.colour or COLOUR_NEUTRAL
        return [
            gr.update(value=rw), gr.update(value=rh),
            gr.update(value=bool(seg.lock_aspect)), gr.update(value="free"),
            gr.update(value=float(seg.speed_f)), gr.update(value=bool(seg.reverse)),
            gr.update(value=col.get("brightness", COLOUR_NEUTRAL["brightness"])),
            gr.update(value=col.get("contrast", COLOUR_NEUTRAL["contrast"])),
            gr.update(value=col.get("saturation", COLOUR_NEUTRAL["saturation"])),
            gr.update(value=col.get("hue", COLOUR_NEUTRAL["hue"])),
            gr.update(value=col.get("warmth", COLOUR_NEUTRAL["warmth"])),
            gr.update(value=col.get("gamma", COLOUR_NEUTRAL["gamma"])),
        ]

    def _save_interactivity(self):
        """(save_inplace, save_copy) interactivity updates — enabled once a
        segment is loaded."""
        has = self._track.selected() is not None
        return gr.update(interactive=has), gr.update(interactive=has)

    # -- wiring (§7.5–7.7) --------------------------------------------------
    def _wire(self, c):
        # ---- timeline bridge (§7.5) ---------------------------------------
        # Py -> JS: run applyOp in the browser, no server round-trip.
        self.tl_from_py.change(fn=None, inputs=[self.tl_from_py], outputs=[],
                               js=tw.APPLY_OP_JS, show_progress="hidden")
        # JS -> Py: full edit JSON on debounced change. Selecting a clip lands
        # here and loads its edits into the tools + Result.
        self.tl_to_py.change(
            self._on_tl_change, inputs=[self.tl_to_py],
            outputs=[self.result_video, self.result_info, *self._tool_outs,
                     self.undo_btn, self.redo_btn,
                     self.save_inplace_btn, self.save_copy_btn],
            show_progress="hidden")

        # ---- crop bridge (§7.5) -------------------------------------------
        # Crop JS -> Py: {seg_id,x,y,w,h} (source px).
        self.crop_to_py.change(
            self._on_crop, inputs=[self.crop_to_py],
            outputs=[self.tl_from_py, self.result_video, self.result_info],
            show_progress="hidden")

        # ---- per-clip tools (§7.6) ----------------------------------------
        tool_outs = [self.tl_from_py, self.result_video, self.result_info,
                     self.undo_btn, self.redo_btn,
                     self.save_inplace_btn, self.save_copy_btn]
        c["flip_h_btn"].click(self._toggle_flip_h, outputs=tool_outs)
        c["flip_v_btn"].click(self._toggle_flip_v, outputs=tool_outs)
        c["reverse_chk"].change(self._set_reverse, inputs=[c["reverse_chk"]],
                                outputs=tool_outs)
        c["speed"].change(self._set_speed, inputs=[c["speed"]], outputs=tool_outs)
        c["resize_btn"].click(
            self._apply_resize,
            inputs=[c["rs_w"], c["rs_h"], c["rs_lock"], c["rs_aspect"]],
            outputs=tool_outs)
        colour_inputs = [c["col_bri"], c["col_con"], c["col_sat"],
                         c["col_hue"], c["col_warm"], c["col_gamma"]]
        for slider in colour_inputs:
            slider.change(self._set_colour, inputs=colour_inputs, outputs=tool_outs)
        # Reset colour also resets the six sliders, so extend its outputs.
        c["col_reset_btn"].click(
            self._reset_colour,
            outputs=tool_outs + colour_inputs)

        # ---- crop toggle (§7.6) -------------------------------------------
        c["crop_toggle"].click(
            self._toggle_crop,
            outputs=[self.crop_panel, self.crop_from_py])

        # ---- splice / rejoin / undo / redo (§7.7) -------------------------
        struct_outs = [self.tl_from_py, self.result_video, self.result_info,
                       self.undo_btn, self.redo_btn,
                       self.save_inplace_btn, self.save_copy_btn, self.status_md]
        c["splice_btn"].click(self._splice, outputs=struct_outs)
        c["rejoin_btn"].click(self._rejoin, outputs=struct_outs)
        # Undo/Redo must reload the tool row too (restored state), so they target
        # the full refresh-style set: result + *TOOL_OUTS + undo/redo + saves.
        hist_outs = ([self.tl_from_py, self.result_video, self.result_info]
                     + list(self._tool_outs)
                     + [self.undo_btn, self.redo_btn,
                        self.save_inplace_btn, self.save_copy_btn])
        c["undo_btn"].click(self._undo, outputs=hist_outs)
        c["redo_btn"].click(self._redo, outputs=hist_outs)

        # ---- save / send (§7.8) -------------------------------------------
        c["save_inplace_btn"].click(self._save_inplace, outputs=[self.status_md])
        c["save_copy_btn"].click(self._save_copy, inputs=[self.save_name],
                                 outputs=[self.status_md])
        c["send_clip_btn"].click(
            self._send_clip, inputs=[self.send_target, self.state],
            outputs=[getattr(self, "main_tabs", self.send_clip_btn),
                     getattr(self, "refresh_form_trigger", self.status_md)])

    # -- bridge handlers (§7.5) ---------------------------------------------
    def _tool_change_return(self):
        """The 7-tuple every per-clip tool handler returns:
        (tl_from_py, result_video, result_info, undo, redo, save_inplace, save_copy)."""
        env = self._env_after()
        seg = self._track.selected()
        rp = self._render_selected()
        si, sc = self._save_interactivity()
        return (env, rp, self._result_info(seg),
                gr.update(interactive=self._track.can_undo()),
                gr.update(interactive=self._track.can_redo()), si, sc)

    def _tool_noop_return(self):
        """No-op variant (no envelope rebuilt) for guarded tool handlers."""
        seg = self._track.selected()
        si, sc = self._save_interactivity()
        return (gr.update(), gr.update(), self._result_info(seg),
                gr.update(interactive=self._track.can_undo()),
                gr.update(interactive=self._track.can_redo()), si, sc)

    def _on_tl_change(self, payload: str):
        """JS -> Py: a debounced full edit-JSON arrived. Apply onto the track,
        fork undo ONLY when the pixel content changed (a pure selection/playhead
        change must not), re-render the now-selected segment, and reload the tool
        row + Result. Returns 1:1 with [result_video, result_info, *TOOL_OUTS,
        undo, redo, save_inplace, save_copy]."""
        if payload:
            old_doc = self._track._document()
            try:
                self._track.from_json(json.loads(payload))
            except Exception:
                traceback.print_exc()
            else:
                new_sig = self._track.content_sig()
                if new_sig != self._last_sig:
                    # Push the PRE-change doc onto undo (cap 30, clear redo).
                    self._track._undo.append(old_doc)
                    if len(self._track._undo) > 30:
                        del self._track._undo[: len(self._track._undo) - 30]
                    self._track._redo.clear()
                    self._last_sig = new_sig
        seg = self._track.selected()
        rp = self._render_selected()
        si, sc = self._save_interactivity()
        return (rp, self._result_info(seg), *self._selection_values(seg),
                gr.update(interactive=self._track.can_undo()),
                gr.update(interactive=self._track.can_redo()), si, sc)

    def _on_crop(self, payload: str):
        """Crop JS -> Py: {seg_id,x,y,w,h} (source px). Apply to that segment
        (set_edit rounds w/h even, clamps to source), re-render, return
        (tl_from_py, result_video, result_info)."""
        if not payload:
            return gr.update(), gr.update(), gr.update()
        try:
            d = json.loads(payload)
        except Exception:
            return gr.update(), gr.update(), gr.update()
        seg_id = d.get("seg_id")
        if not seg_id or self._track.index_of(seg_id) < 0:
            return gr.update(), gr.update(), gr.update()
        crop_rect = {"x": d.get("x", 0), "y": d.get("y", 0),
                     "w": d.get("w", 0), "h": d.get("h", 0)}
        self._track.snapshot()
        self._track.set_edit(seg_id, crop=crop_rect)
        env = self._env_after()
        rp = self._render_selected()
        return env, rp, self._result_info(self._track.selected())

    # -- per-clip tool handlers (§7.6) --------------------------------------
    def _require_selection(self):
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Load a clip and select a segment first.")
        return sel

    def _toggle_flip_h(self):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, flip_h=not sel.flip_h)
        return self._tool_change_return()

    def _toggle_flip_v(self):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, flip_v=not sel.flip_v)
        return self._tool_change_return()

    def _set_reverse(self, reverse):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        if bool(reverse) == bool(sel.reverse):
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, reverse=bool(reverse))
        return self._tool_change_return()

    def _set_speed(self, speed):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        try:
            sp = float(speed or 1.0)
        except (TypeError, ValueError):
            sp = 1.0
        if abs(sp - sel.speed_f) < 1e-6:
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, speed=sp)
        return self._tool_change_return()

    def _apply_resize(self, rs_w, rs_h, rs_lock, rs_aspect):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        w, h = self._resolve_resize(rs_w, rs_h, rs_lock, rs_aspect,
                                    sel.src_w, sel.src_h)
        if w is None and h is None:
            gr.Warning("Set a width and/or height (or pick an aspect preset).")
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, resize={"w": w, "h": h},
                             lock_aspect=bool(rs_lock))
        gr.Info("Resize applied.")
        return self._tool_change_return()

    @staticmethod
    def _resolve_resize(rs_w, rs_h, rs_lock, rs_aspect, src_w, src_h):
        """Resolve a resize target (w, h); either may be None for auto. An aspect
        preset other than 'free' derives the missing dim from the present one (or
        from the source size). 0/blank are treated as unset."""
        def _num(v):
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                return None
            return n if n > 0 else None

        w = _num(rs_w)
        h = _num(rs_h)
        ratios = {"1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4,
                  "16:9": 16 / 9, "9:16": 9 / 16}
        ratio = ratios.get(rs_aspect)
        if ratio:
            # An explicit aspect: derive the missing dim from the present one,
            # else fit the source's larger side.
            if w and not h:
                h = int(round(w / ratio))
            elif h and not w:
                w = int(round(h * ratio))
            elif not w and not h and src_w and src_h:
                w = int(src_w)
                h = int(round(w / ratio))
        elif rs_lock and w and h:
            h = None  # width drives; the model auto-fits the height to source AR
        return w, h

    def _set_colour(self, bri, con, sat, hue, warm, gamma):
        sel = self._require_selection()
        if sel is None:
            return self._tool_noop_return()
        new_col = {"brightness": bri, "contrast": con, "saturation": sat,
                   "hue": hue, "warmth": warm, "gamma": gamma}
        # No-op if the sliders match the current colour (avoids junk undo states
        # when a slider .change fires on selection-load).
        cur = sel.colour or COLOUR_NEUTRAL
        try:
            same = all(abs(float(new_col[k]) - float(cur.get(k, COLOUR_NEUTRAL[k])))
                       < 1e-6 for k in COLOUR_NEUTRAL)
        except (TypeError, ValueError):
            same = False
        if same:
            return self._tool_noop_return()
        self._track.snapshot()
        self._track.set_edit(sel.id, colour=new_col)
        return self._tool_change_return()

    def _reset_colour(self):
        sel = self._require_selection()
        neutral_sliders = [
            gr.update(value=COLOUR_NEUTRAL["brightness"]),
            gr.update(value=COLOUR_NEUTRAL["contrast"]),
            gr.update(value=COLOUR_NEUTRAL["saturation"]),
            gr.update(value=COLOUR_NEUTRAL["hue"]),
            gr.update(value=COLOUR_NEUTRAL["warmth"]),
            gr.update(value=COLOUR_NEUTRAL["gamma"]),
        ]
        if sel is None:
            return (*self._tool_noop_return(), *neutral_sliders)
        if sel.is_neutral_colour:
            return (*self._tool_noop_return(), *neutral_sliders)
        self._track.snapshot()
        self._track.set_edit(sel.id, colour=dict(COLOUR_NEUTRAL))
        gr.Info("Colour reset.")
        return (*self._tool_change_return(), *neutral_sliders)

    def _toggle_crop(self):
        """Flip the crop panel's visibility. When OPENING, push the selected
        segment's source frame into the canvas via the one-shot injector."""
        if self._crop_open:
            self._crop_open = False
            return gr.update(visible=False), gr.update()
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Select a clip first to crop it.")
            return gr.update(visible=False), gr.update()
        frame_uri = render.extract_frame(sel)
        if not frame_uri:
            gr.Warning("Couldn't extract a frame to crop — see the console.")
            return gr.update(visible=False), gr.update()
        html = crop.crop_bridge_html(frame_uri, sel.id, sel.src_w, sel.src_h,
                                     nonce=str(time.time()))
        self._crop_open = True
        return gr.update(visible=True), gr.update(value=html)

    # -- splice / rejoin / undo / redo (§7.7) -------------------------------
    def _struct_return(self, status: str, *, rebaseline: bool):
        """The 8-tuple splice/rejoin return: (tl_from_py, result_video,
        result_info, undo, redo, save_inplace, save_copy, status_md)."""
        env = self._env_after() if rebaseline else self._load_envelope()
        seg = self._track.selected()
        rp = self._render_selected()
        si, sc = self._save_interactivity()
        return (env, rp, self._result_info(seg),
                gr.update(interactive=self._track.can_undo()),
                gr.update(interactive=self._track.can_redo()),
                si, sc, status)

    def _splice(self):
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Select a clip to splice.")
            return self._struct_return("⚠️ Nothing selected.", rebaseline=False)
        at_src = self._playhead_src_sec(sel)
        self._track.snapshot()
        ids = self._track.splice(sel.id, at_src)
        if not ids:
            # No-op cut (too close to an edge): drop the junk undo snapshot.
            if self._track._undo:
                self._track._undo.pop()
            gr.Warning("Move the playhead inside the clip to splice.")
            return self._struct_return("⚠️ Playhead isn't inside the clip.",
                                       rebaseline=False)
        gr.Info("Spliced.")
        return self._struct_return(f"✂ Spliced at {at_src:.2f}s.", rebaseline=True)

    def _rejoin(self):
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Select a clip to rejoin.")
            return self._struct_return("⚠️ Nothing selected.", rebaseline=False)
        self._track.snapshot()
        ok = self._track.rejoin(sel.id)
        if not ok:
            if self._track._undo:
                self._track._undo.pop()
            gr.Warning("No contiguous same-source clip to rejoin.")
            return self._struct_return("⚠️ Nothing to rejoin.", rebaseline=False)
        gr.Info("Rejoined.")
        return self._struct_return("⛓ Rejoined adjacent clips.", rebaseline=True)

    def _history_return(self):
        """The undo/redo return: (tl_from_py, result_video, result_info,
        *TOOL_OUTS, undo, redo, save_inplace, save_copy)."""
        env = self._env_after()
        seg = self._track.selected()
        rp = self._render_selected()
        si, sc = self._save_interactivity()
        return (env, rp, self._result_info(seg), *self._selection_values(seg),
                gr.update(interactive=self._track.can_undo()),
                gr.update(interactive=self._track.can_redo()), si, sc)

    def _undo(self):
        if not self._track.undo():
            gr.Warning("Nothing to undo.")
        else:
            gr.Info("Undone.")
        return self._history_return()

    def _redo(self):
        if not self._track.redo():
            gr.Warning("Nothing to redo.")
        else:
            gr.Info("Redone.")
        return self._history_return()

    # -- save / send (§7.8) -------------------------------------------------
    @staticmethod
    def _is_canonical_source(path: str) -> bool:
        """True when ``path`` looks like a real on-disk source we may overwrite —
        i.e. NOT a Gradio temp / upload scratch file. Uploads route to a copy."""
        if not path or not os.path.exists(path):
            return False
        rp = os.path.realpath(path)
        bad = []
        try:
            import tempfile
            bad.append(os.path.realpath(os.path.join(tempfile.gettempdir(), "gradio")))
        except Exception:
            pass
        try:
            from gradio.utils import get_upload_folder
            bad.append(os.path.realpath(get_upload_folder()))
        except Exception:
            pass
        for b in bad:
            try:
                if os.path.commonpath([rp, b]) == b:
                    return False
            except (ValueError, OSError):
                continue
        return True

    def _save_inplace(self):
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Load and edit a clip first.")
            return "⚠️ Load and edit a clip first."
        render_path = self._render_selected()
        if not render_path:
            return "❌ Render failed — couldn't produce a file to save."
        source = sel.source
        if not self._is_canonical_source(source):
            # Uploads / temp sources have no canonical original — save a copy.
            try:
                dest = paths.save_as_copy(render_path, source, "", self._save_path())
            except Exception as exc:
                traceback.print_exc()
                gr.Warning("Save failed — see the console.")
                return f"❌ Save failed: {exc}"
            gr.Info("Source has no canonical original — saved a copy instead.")
            return (f"📑 No overwrite target: saved a copy → `{dest}` "
                    "(appears in the gallery after the next refresh).")
        try:
            paths.save_in_place(render_path, source)
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("Save failed — see the console.")
            return f"❌ Save failed: {exc}"
        gr.Info("Saved — overwrote the original.")
        return (f"💾 Overwrote original → `{source}`. The gallery thumbnail "
                "refreshes on the next reselect/regenerate.")

    def _save_copy(self, name):
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Load and edit a clip first.")
            return "⚠️ Load and edit a clip first."
        render_path = self._render_selected()
        if not render_path:
            return "❌ Render failed — couldn't produce a file to save."
        try:
            dest = paths.save_as_copy(render_path, sel.source, name, self._save_path())
        except Exception as exc:
            traceback.print_exc()
            gr.Warning("Save failed — see the console.")
            return f"❌ Save failed: {exc}"
        gr.Info("Saved a copy to the outputs folder.")
        return (f"📑 Saved copy → `{dest}` "
                "(appears in the gallery after the next refresh).")

    def _send_clip(self, label, state_val):
        """Hand the whole edited clip (the selected segment's render) to the
        Media Generator's Continue-Video (``video_source``) source, or to a
        path-payload SendTo receiver (e.g. Reel2Reel). Returns
        ``(main_tabs_update, form_trigger_stamp)``."""
        noop = gr.update()
        sel = self._track.selected()
        if sel is None:
            gr.Warning("Load and edit a clip first.")
            return noop, noop
        spec = (self._clip_targets_by_label or {}).get(label)
        if spec is None:
            gr.Warning("Unknown target.")
            return noop, noop
        render_path = self._render_selected()
        if not render_path:
            return noop, noop

        if spec.get("kind") == "continue":
            # Continue Video: set the generator's video_source + ensure the
            # video-continuation flag ("V" in image_prompt_type), navigate to the
            # generator and poke the form trigger (Reel2Reel's pattern).
            try:
                s = self.get_current_model_settings(state_val)
                s["video_source"] = render_path
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

        # path-payload plugin receiver (e.g. Reel2Reel) via the SendTo contract.
        if not callable(getattr(self, "_sendto_enqueue", None)):
            gr.Warning("SendTo isn't installed — can't hand off to that plugin.")
            return noop, noop
        try:
            self._sendto_enqueue(state_val, spec.get("inbox_key"), render_path,
                                 payload="path")
        except Exception:
            traceback.print_exc()
            gr.Warning("Could not hand the clip over.")
            return noop, noop
        gr.Info(f"Sent the edited clip to {label} (loads when you open the tab).")
        tab = spec.get("tab")
        return (gr.update(selected=f"plugin_{tab}") if tab else noop), noop

    # -- on tab select / inbox (§7.3) ---------------------------------------
    def on_tab_select(self, state: dict):
        """Drain the SendTo inbox on every tab entry. Ingest the most-recent
        queued path — probing it, recording its source-duration ceiling, then
        loading (fresh track) or appending (extend an existing track). Returns
        values 1:1 with self.on_tab_outputs; a no-op refresh when nothing queued."""
        try:
            items = inbox.drain(state)
            if items:
                path = items[-1]
                probe_info = ffmpeg.probe(path, get_video_info=self._gvi())
                self._src_dur[path] = float(probe_info.get("duration") or 0.0)
                if self._track.segments:
                    self._track.snapshot()
                    self._track.append_source(path, probe_info)
                else:
                    self._track.load_source(path, probe_info)
                self._last_sig = self._track.content_sig()
                seg = self._track.selected()
                rp = self._render_selected()
                si, sc = self._save_interactivity()
                return [self._load_envelope(), rp, self._result_info(seg),
                        path, *self._selection_values(seg),
                        gr.update(interactive=self._track.can_undo()),
                        gr.update(interactive=self._track.can_redo()), si, sc]
        except Exception:
            traceback.print_exc()
        # Nothing queued (or a probe failed): no-op updates aligned 1:1 with
        # on_tab_outputs.
        return [gr.update() for _ in self.on_tab_outputs]
