"""Layout for the Mutator tab — components only, no event wiring.

Mutator v0.3 is a per-clip single-track editor modelled on Reel2Reel: **the
preview frame IS the editing stage**. Top to bottom:

* **STAGE** — the crop canvas (``ui.crop``) rendered as the always-on primary
  surface: the selected clip's source frame at the playhead, with a draggable
  crop rectangle + aspect presets drawn directly on it. Scrubbing the timeline
  changes the frame; dragging the rectangle sets the crop in source pixels.
* **TIMELINE** — the draggable single-track timeline (``ui.timeline``): one
  ordered track of Segments, click-to-select, drag-trim edges, playhead, splice.
* **LOAD / STRUCTURE row** — load a clip (OS file browser, gallery selection, or
  a SendTo hand-off) + Splice / Rejoin.
* **INSPECTOR** — one panel that loads the SELECTED clip's edits (speed, reverse,
  flip, resize, colour) and mutates only that segment; Undo / Redo.
* **RESULT** — a ``gr.Video`` playing the rendered selected segment + info.
* **SEND** — save in place / as copy, the native SendTo frame panel, "Send
  edited clip".

:func:`build_ui` returns a flat ``{key: gr.Component}`` dict; ``plugin.py``'s
``_wire`` attaches every handler, so this module stays a pure view. The timeline
+ crop mounts come from the widget factories (their bridge elem_ids live there);
their dicts are spread into the returned dict. The banner is injected by
``plugin.create_ui``, not here.

Colour sliders store UI units; ``core/render`` maps them to ffmpeg:
    brightness = (bri - 100) / 100      contrast   = con / 100
    saturation = sat / 100              gamma      = gamma (raw)
    hue        = hue degrees (raw)      warmth     = warmth (raw)
"""
from __future__ import annotations

import gradio as gr

from .crop import build_crop_widget
from .timeline import build_timeline_widget

# Aspect-ratio presets shared by the crop canvas and the resize tool.
ASPECT_CHOICES = ["free", "1:1", "4:3", "3:4", "16:9", "9:16"]

# The selection-driven tool outputs, in the fixed order ``_selection_values(seg)``
# returns updates for. plugin.py builds TOOL_OUTS from these keys so a selection
# change repopulates the inspector 1:1.
TOOL_OUT_KEYS = [
    "rs_w", "rs_h", "rs_lock", "rs_aspect", "speed", "reverse_chk",
    "col_bri", "col_con", "col_sat", "col_hue", "col_warm", "col_gamma",
]


def build_ui() -> dict:
    """Build the Mutator v0.3 tab body and return its flat component dict.

    The returned dict ``c`` exposes every key ``plugin.py`` wires. The timeline +
    crop widget dicts are spread in, so ``c`` also carries
    ``tl_mount/tl_to_py/tl_from_py`` and ``crop_mount/crop_to_py/crop_from_py``.
    """
    c: dict = {}

    # ======================================================================
    #  STAGE — the crop canvas as the always-on primary surface
    # ======================================================================
    with gr.Column(elem_id="mutator-stage"):
        gr.Markdown(
            "**Stage** — drag the rectangle to crop · scrub the timeline below to "
            "change the frame", elem_classes="mutator-stage-caption")
        # build_crop_widget() spreads crop_mount (the iframe) + the two bridge
        # pipes (mut_crop_to_py / mut_crop_from_py). No panel/toggle: it is the
        # main editing surface now.
        c.update(build_crop_widget())

    # ======================================================================
    #  TIMELINE — draggable single track
    # ======================================================================
    with gr.Column(elem_id="mutator-timeline"):
        # tl_mount + the hidden bridge textboxes (mut_tl_root/to_py/from_py).
        c.update(build_timeline_widget())

    # ======================================================================
    #  LOAD / STRUCTURE row
    # ======================================================================
    with gr.Row(elem_id="mutator-loadrow"):
        c["upload_btn"] = gr.UploadButton(
            "📁 Load file…", file_types=["video"], file_count="single",
            size="sm", variant="primary")
        c["load_gallery_btn"] = gr.Button(
            "⟳ From gallery selection", size="sm")
        c["splice_btn"] = gr.Button("✂ Splice", size="sm")
        c["rejoin_btn"] = gr.Button("⛓ Rejoin", size="sm")

    # ======================================================================
    #  INSPECTOR — edits for the SELECTED clip (greyed until one is loaded)
    # ======================================================================
    with gr.Group(elem_id="mutator-inspector"):
        gr.Markdown("**Selected clip**", elem_classes="mutator-inspector-title")
        with gr.Row():
            c["speed"] = gr.Slider(0.1, 8.0, value=1.0, step=0.05, label="Speed ×")
            c["reverse_chk"] = gr.Checkbox(label="Reverse", value=False)
            c["flip_h_btn"] = gr.Button("⇋ Flip H", size="sm")
            c["flip_v_btn"] = gr.Button("⇵ Flip V", size="sm")
        with gr.Row():
            c["rs_w"] = gr.Number(label="W", value=None, precision=0, minimum=0)
            c["rs_h"] = gr.Number(label="H", value=None, precision=0, minimum=0)
            c["rs_lock"] = gr.Checkbox(label="Lock", value=True)
            c["rs_aspect"] = gr.Radio(ASPECT_CHOICES, value="free", label="Aspect")
            c["resize_btn"] = gr.Button("Apply resize", size="sm")
        with gr.Accordion("🎨 Colour", open=False, elem_id="mutator-colour"):
            with gr.Row():
                c["col_bri"] = gr.Slider(50, 150, value=100, step=1, label="Bright")
                c["col_con"] = gr.Slider(50, 150, value=100, step=1, label="Contrast")
                c["col_sat"] = gr.Slider(0, 200, value=100, step=1, label="Sat")
            with gr.Row():
                c["col_hue"] = gr.Slider(-180, 180, value=0, step=1, label="Hue")
                c["col_warm"] = gr.Slider(-100, 100, value=0, step=1, label="Warmth")
                c["col_gamma"] = gr.Slider(0.5, 2.0, value=1.0, step=0.01, label="Gamma")
            c["col_reset_btn"] = gr.Button("Reset colour", size="sm")
        with gr.Row():
            c["undo_btn"] = gr.Button("↶ Undo", size="sm", interactive=False)
            c["redo_btn"] = gr.Button("↷ Redo", size="sm", interactive=False)

    # ======================================================================
    #  RESULT — playback of the rendered selected segment
    # ======================================================================
    with gr.Column(elem_id="mutator-result"):
        c["result_video"] = gr.Video(label="Result (selected clip render)",
                                     interactive=False)
        c["result_info"] = gr.Markdown("")

    # ======================================================================
    #  SEND
    # ======================================================================
    with gr.Column(elem_id="mutator-send"):
        c["save_name"] = gr.Textbox(
            label="Save-as-copy name",
            placeholder="(defaults to <source>_edited)")
        with gr.Row():
            c["save_inplace_btn"] = gr.Button(
                "💾 Save in place", variant="stop", interactive=False)
            c["save_copy_btn"] = gr.Button(
                "📑 Save as copy", variant="primary", interactive=False)

        # Native SendTo frame-panel slot — filled in create_ui via
        # ui.sendout.build_send_panel (no external sendto package needed).
        with gr.Column(elem_id="mutator-send-frame") as send_frame_slot:
            pass
        c["send_frame_slot"] = send_frame_slot

        # Send the edited CLIP onward (Continue Video / path receivers). The
        # plugin fills the dropdown choices in create_ui. Wide scale so labels
        # never truncate to a single letter.
        with gr.Row():
            c["send_target"] = gr.Dropdown(
                label="Send edited clip to", choices=[], interactive=True,
                scale=3)
            c["send_clip_btn"] = gr.Button(
                "Send edited clip →", variant="primary", scale=1)

        c["status_md"] = gr.Markdown("")

    return c
