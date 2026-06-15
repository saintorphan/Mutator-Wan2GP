"""Layout for the Mutator tab — components only, no event wiring.

Mutator v0.2 is a per-clip single-track video editor laid out as three vertical
zones inside one tab:

* **WORKSPACE** — the source ``gr.Video`` player, a single compact tool row (no
  accordion stacks), the draggable single-track timeline mount, and a togglable
  crop-canvas mount. The timeline is one ordered track of Segments; selecting a
  segment loads ITS OWN edits into the tools + the Result player.
* **RESULT** — a ``gr.Video`` of the SELECTED segment's render + an info readout.
* **SEND** — save-in-place / save-as-copy, the embedded SendTo frame-panel slot,
  and the "Send edited clip" panel.

:func:`build_ui` constructs the tab body and returns a flat ``{key: gr.Component}``
dict; ``plugin.py``'s ``_wire`` consumes it to attach every handler, so this
module stays a pure view — it never imports the host or touches state.

The timeline + crop mounts come from the widget factories in :mod:`ui.timeline`
and :mod:`ui.crop`; their component dicts are spread into the returned dict under
the keys those factories define (``tl_mount/tl_to_py/tl_from_py`` and
``crop_mount/crop_to_py/crop_from_py``) so the JS↔Python bridge elem_ids stay in
one place. The banner ``gr.HTML`` is injected by ``plugin.create_ui``, not here.

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

# The selection-driven tool-row outputs, in the fixed order the plugin's
# ``_selection_values(seg)`` returns updates for. plugin.py builds TOOL_OUTS from
# these dict keys (rs_w, rs_h, rs_lock, rs_aspect, speed, reverse_chk, then the
# six colour sliders) so a selection change repopulates the row 1:1.
TOOL_OUT_KEYS = [
    "rs_w", "rs_h", "rs_lock", "rs_aspect", "speed", "reverse_chk",
    "col_bri", "col_con", "col_sat", "col_hue", "col_warm", "col_gamma",
]


def build_ui() -> dict:
    """Build the Mutator v0.2 tab body and return its flat component dict.

    The returned dict ``c`` exposes every key ``plugin.py`` wires (see §6.3 of the
    implementation spec / the module docstring for the binding contract). The
    timeline + crop widget dicts are spread in, so ``c`` also carries
    ``tl_mount/tl_to_py/tl_from_py`` and ``crop_mount/crop_to_py/crop_from_py``.
    """
    c: dict = {}

    # ---- WORKSPACE --------------------------------------------------------
    with gr.Column(elem_id="mutator-workspace"):
        # The source player. Read-only: loading happens via the gallery / upload
        # button / SendTo hand-off, not by dropping into this player.
        c["src_video"] = gr.Video(label="Source", interactive=False)

        # Compact tool row — every core tool on one row, NO accordions. Each
        # handler mutates the SELECTED segment, re-renders, and returns.
        with gr.Row(elem_id="mutator-tools"):
            c["splice_btn"] = gr.Button("✂ Splice", size="sm")
            c["rejoin_btn"] = gr.Button("⛓ Rejoin", size="sm")
            c["crop_toggle"] = gr.Button("⛶ Crop", size="sm")
            c["flip_h_btn"] = gr.Button("⇋ Flip H", size="sm")
            c["flip_v_btn"] = gr.Button("⇵ Flip V", size="sm")
            c["reverse_chk"] = gr.Checkbox(label="Reverse", value=False)
            c["undo_btn"] = gr.Button("↶ Undo", size="sm", interactive=False)
            c["redo_btn"] = gr.Button("↷ Redo", size="sm", interactive=False)

        # Speed (timeline-length affecting) — its own row so the slider breathes.
        with gr.Row(elem_id="mutator-speed"):
            c["speed"] = gr.Slider(0.1, 8.0, value=1.0, step=0.05,
                                   label="Speed ×")

        # Resize sub-group: w/h + lock + aspect presets + apply. The aspect
        # presets here drive the RESIZE op and are independent of the crop
        # canvas's own in-iframe aspect buttons.
        with gr.Row(elem_id="mutator-resize"):
            c["rs_w"] = gr.Number(label="W", value=None, precision=0,
                                  minimum=0)
            c["rs_h"] = gr.Number(label="H", value=None, precision=0,
                                  minimum=0)
            c["rs_lock"] = gr.Checkbox(label="Lock", value=True)
            c["rs_aspect"] = gr.Radio(ASPECT_CHOICES, value="free",
                                      label="Aspect")
            c["resize_btn"] = gr.Button("Apply resize", size="sm")

        # Colour: six compact sliders + reset, tucked in a closed Accordion so
        # the row stays tight. (This is a colour pop-out, not a core-tool stack —
        # the core trim/splice/transform tools all sit on the open rows above.)
        with gr.Accordion("🎨 Colour", open=False,
                          elem_id="mutator-colour"):
            with gr.Row():
                c["col_bri"] = gr.Slider(50, 150, value=100, step=1,
                                         label="Bright")
                c["col_con"] = gr.Slider(50, 150, value=100, step=1,
                                         label="Contrast")
                c["col_sat"] = gr.Slider(0, 200, value=100, step=1,
                                         label="Sat")
            with gr.Row():
                c["col_hue"] = gr.Slider(-180, 180, value=0, step=1,
                                         label="Hue")
                c["col_warm"] = gr.Slider(-100, 100, value=0, step=1,
                                          label="Warmth")
                c["col_gamma"] = gr.Slider(0.5, 2.0, value=1.0, step=0.01,
                                           label="Gamma")
            c["col_reset_btn"] = gr.Button("Reset colour", size="sm")

        # Timeline mount + the two hidden bridge textboxes (JS↔Py). Spread the
        # factory dict so the elem_ids (mut_tl_root / mut_tl_to_py /
        # mut_tl_from_py) live in ui.timeline.
        c.update(build_timeline_widget())

        # Crop canvas mount — hidden until the Crop tool toggles it open. Lives
        # in its own Column so the plugin can flip ``crop_panel`` visibility; the
        # factory spreads the mount + its two bridge pipes (mut_crop_frame /
        # mut_crop_to_py / mut_crop_from_py).
        with gr.Column(visible=False, elem_id="mutator-crop-panel") as crop_panel:
            c.update(build_crop_widget())
        c["crop_panel"] = crop_panel

    # ---- RESULT -----------------------------------------------------------
    with gr.Column(elem_id="mutator-result"):
        c["result_video"] = gr.Video(label="Result (selected clip render)",
                                     interactive=False)
        c["result_info"] = gr.Markdown("")

    # ---- SEND -------------------------------------------------------------
    with gr.Column(elem_id="mutator-send"):
        c["save_name"] = gr.Textbox(
            label="Save-as-copy name",
            placeholder="(defaults to <source>_edited)")
        with gr.Row():
            c["save_inplace_btn"] = gr.Button(
                "💾 Save in place", variant="stop", interactive=False)
            c["save_copy_btn"] = gr.Button(
                "📑 Save as copy", variant="primary", interactive=False)

        # Embedded send FRAME panel slot — the plugin builds the panel inside
        # this Column via the NATIVE ui.sendout.build_send_panel in create_ui
        # (SendTo is vendored in; no external sendto package required).
        with gr.Column(elem_id="mutator-send-frame") as send_frame_slot:
            pass
        c["send_frame_slot"] = send_frame_slot

        # Send the edited CLIP onward (Continue Video / path receivers). The
        # plugin populates the dropdown choices in create_ui.
        with gr.Row():
            c["send_target"] = gr.Dropdown(
                label="Send edited clip to", choices=[], interactive=True)
            c["send_clip_btn"] = gr.Button(
                "➤ Send edited clip", variant="primary")

        c["status_md"] = gr.Markdown("")

    return c
