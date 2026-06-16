"""Layout for the Mutator tab — components only, no event wiring.

Mutator v0.5 is a per-clip single-track editor modelled on Reel2Reel: **the
preview player IS the editing stage**, laid out PREVIEW | RESULT side by side at
the top. Top to bottom:

* **TOP ROW (side by side)**
    * **STAGE** (left) — a real video-preview player (``ui.stage``): the selected
      clip plays with custom transport controls, a draggable crop rectangle +
      aspect presets drawn over the video (visible only in crop mode), and
      playback synced bidirectionally to the timeline playhead. Directly UNDER
      the preview live the **info line** (speed · W×H) and the uniform **TOOL
      ROW** of square icon buttons (crop / resize / speed / flip-h / flip-v /
      colour / splice / rejoin / undo / redo) plus their popups (resize, speed),
      the crop-aspect dropdown and the right-side colour drawer.
    * **RESULT** (right) — a ``gr.Video`` playing the rendered selected segment +
      info.
* **TIMELINE** — the draggable single-track timeline (``ui.timeline``): one
  ordered track of Segments, click-to-select, drag-trim edges, playhead, splice.
* **LOAD row** — load a clip (OS file browser, gallery selection, or a SendTo
  hand-off).
* **SEND** — save in place / as copy, the native SendTo frame panel, "Send
  edited clip".

:func:`build_ui` returns a flat ``{key: gr.Component}`` dict; ``plugin.py``'s
``_wire`` attaches every handler, so this module stays a pure view. The timeline
+ stage mounts come from the widget factories (their bridge elem_ids live there);
their dicts are spread into the returned dict. The banner is injected by
``plugin.create_ui``, not here.

Colour sliders store UI units; ``core/render`` maps them to ffmpeg:
    brightness = (bri - 100) / 100      contrast   = con / 100
    saturation = sat / 100              gamma      = gamma (raw)
    hue        = hue degrees (raw)      warmth     = warmth (raw)
"""
from __future__ import annotations

import gradio as gr

from .stage import build_stage_widget
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
    """Build the Mutator v0.5 tab body and return its flat component dict.

    The returned dict ``c`` exposes every key ``plugin.py`` wires. The timeline +
    stage widget dicts are spread in, so ``c`` also carries
    ``tl_mount/tl_to_py/tl_from_py`` and ``stage_mount/crop_to_py/stage_from_py``.
    """
    c: dict = {}

    # ======================================================================
    #  TOP ROW — STAGE (video preview player + tool row) | RESULT
    # ======================================================================
    with gr.Row(elem_id="mutator-top"):
        # -- LEFT: the video-preview stage (transport + crop overlay) ------
        with gr.Column(elem_id="mutator-stage"):
            # The "PREVIEW" zone header (CSS #mutator-stage::before) already
            # labels this column; no extra caption Markdown needed.
            # build_stage_widget() spreads stage_mount (#mut_stage_root) + the
            # crop pipe (mut_crop_to_py) + the clip injector (mut_stage_from_py).
            c.update(build_stage_widget())

            # ---- INFO LINE (speed · W×H of the selected clip) -------------
            c["stage_info"] = gr.Markdown("", elem_id="mutator-stage-info")

            # ---- TOOL ROW — uniform square icon buttons (same size as the
            #      transport). Every button carries elem_classes=["mut-tool"]
            #      so the CSS sizes them 40×36 uniformly. ----------------------
            with gr.Row(elem_id="mutator-tools"):
                c["crop_btn"] = gr.Button(
                    "⛶", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-crop")
                c["resize_btn"] = gr.Button(
                    "⤢", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-resize")
                c["speed_btn"] = gr.Button(
                    "⏩︎", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-speed")
                # Clear mirror glyphs: ◧ horizontal-flip, ⬓ vertical-flip.
                c["flip_h_btn"] = gr.Button(
                    "◧", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-fliph")
                c["flip_v_btn"] = gr.Button(
                    "⬓", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-flipv")
                # ◐ (half-filled circle) is an inherently monochrome glyph that
                # reads as a colour/tone control and matches the transport weight.
                c["color_btn"] = gr.Button(
                    "◐", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-color")
                c["splice_btn"] = gr.Button(
                    "✂︎", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-splice")
                c["rejoin_btn"] = gr.Button(
                    "⛓︎", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-rejoin")
                c["undo_btn"] = gr.Button(
                    "↶", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-undo",
                    interactive=False)
                c["redo_btn"] = gr.Button(
                    "↷", elem_classes=["mut-tool"], size="sm",
                    elem_id="mut-tool-redo",
                    interactive=False)

            # ---- CROP drawer (right-side; toggled by crop_btn) -------------
            # Mirrors the colour drawer but with aspect-ratio choices. Opening it
            # activates crop mode on the stage; picking an aspect constrains the
            # draggable crop rectangle live (JS -> MutStage.setAspect).
            with gr.Group(elem_id="mutator-crop-drawer", visible=False) \
                    as crop_drawer:
                gr.Markdown("**Crop**", elem_classes="mutator-pop-title")
                gr.Markdown(
                    "Drag the rectangle on the preview. Pick an aspect to lock it:",
                    elem_classes="mutator-pop-hint")
                c["crop_aspect"] = gr.Radio(
                    ASPECT_CHOICES, value="free", label="Aspect",
                    elem_id="mutator-crop-aspect")
            c["crop_drawer"] = crop_drawer

            # ---- RESIZE popup (toggled by resize_btn) ---------------------
            with gr.Group(elem_id="mutator-resize-pop", visible=False) \
                    as resize_pop:
                gr.Markdown("**Resize**", elem_classes="mutator-pop-title")
                with gr.Row():
                    c["rs_aspect"] = gr.Dropdown(
                        ASPECT_CHOICES, value="free", label="Aspect")
                with gr.Row():
                    c["rs_w"] = gr.Number(
                        label="W", value=None, precision=0, minimum=0)
                    c["rs_h"] = gr.Number(
                        label="H", value=None, precision=0, minimum=0)
                    c["rs_lock"] = gr.Checkbox(label="Lock", value=True)
                c["apply_resize_btn"] = gr.Button(
                    "Apply", size="sm", variant="primary")
            c["resize_pop"] = resize_pop

            # ---- SPEED popup (toggled by speed_btn) -----------------------
            with gr.Group(elem_id="mutator-speed-pop", visible=False) \
                    as speed_pop:
                gr.Markdown("**Speed**", elem_classes="mutator-pop-title")
                c["speed"] = gr.Slider(
                    0.1, 8.0, value=1.0, step=0.05, label="Speed ×")
                c["reverse_chk"] = gr.Checkbox(label="Reverse", value=False)
            c["speed_pop"] = speed_pop

            # ---- COLOUR drawer (right-side; toggled by color_btn) ---------
            with gr.Group(elem_id="mutator-color-drawer", visible=False) \
                    as color_drawer:
                gr.Markdown("**Colour**", elem_classes="mutator-pop-title")
                c["col_bri"] = gr.Slider(50, 150, value=100, step=1, label="Bright")
                c["col_con"] = gr.Slider(50, 150, value=100, step=1, label="Contrast")
                c["col_sat"] = gr.Slider(0, 200, value=100, step=1, label="Sat")
                c["col_hue"] = gr.Slider(-180, 180, value=0, step=1, label="Hue")
                c["col_warm"] = gr.Slider(-100, 100, value=0, step=1, label="Warmth")
                c["col_gamma"] = gr.Slider(0.5, 2.0, value=1.0, step=0.01, label="Gamma")
                c["col_reset_btn"] = gr.Button("Reset colour", size="sm")
            c["color_drawer"] = color_drawer

        # -- RIGHT: the rendered result of the selected segment ------------
        with gr.Column(elem_id="mutator-result"):
            c["result_video"] = gr.Video(
                label="Result (selected clip render)", interactive=False)
            c["result_info"] = gr.Markdown("")

    # ======================================================================
    #  TIMELINE — draggable single track (full width)
    # ======================================================================
    with gr.Column(elem_id="mutator-timeline"):
        # tl_mount + the hidden bridge textboxes (mut_tl_root/to_py/from_py).
        c.update(build_timeline_widget())

    # ======================================================================
    #  LOAD row
    # ======================================================================
    with gr.Row(elem_id="mutator-loadrow"):
        c["upload_btn"] = gr.UploadButton(
            "📁 Load file…", file_types=["video"], file_count="single",
            size="sm", variant="primary")
        c["load_gallery_btn"] = gr.Button(
            "⟳ From gallery selection", size="sm")

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
