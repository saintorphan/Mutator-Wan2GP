"""Layout for the Mutator tab — components only, no event wiring.

:func:`build_ui` constructs the whole tab body (load row, preview, mini-timeline,
and the edit accordions: Trim & Split, Transform, Resize, Crop, Speed, Colour,
plus History and Save) and returns a flat ``{key: gr.Component}`` dict. The
plugin (``plugin.py``) consumes that dict in ``_wire`` to attach every handler,
so this module stays a pure view — it never imports the host or touches state.

The trim controls degrade gracefully: when ``have_rangeslider`` is true a single
dual-handle :class:`gradio_rangeslider.RangeSlider` is used (frames); otherwise
two plain :class:`gradio.Slider` are emitted under the ``"trim_start"`` /
``"trim_end"`` keys. ``plugin.py``'s ``_read_range`` / ``_range_update`` adapter
(ported from Trimline) handles both shapes transparently.

Colour-slider → ffmpeg mapping used by the plugin wiring:
    eq brightness = (bri - 100) / 100      contrast   = con / 100
    saturation    = sat / 100              gamma      = gamma
    hue           = hue (degrees)          warmth     = warmth
"""
from __future__ import annotations

import gradio as gr

try:  # host ships gradio_rangeslider; degrade to two plain sliders if absent
    from gradio_rangeslider import RangeSlider
except Exception:  # pragma: no cover
    RangeSlider = None

# Common resize presets surfaced as a Radio (label -> (w, h); "Auto" keeps aspect).
_RESIZE_PRESETS = [
    "Keep",
    "1920×1080",
    "1280×720",
    "1080×1920",
    "720×1280",
    "1024×1024",
    "512×512",
]


def build_ui(have_rangeslider: bool) -> dict:
    """Build the Mutator tab body and return its component dict.

    *have_rangeslider* selects the trim control shape (one ``RangeSlider`` vs two
    ``gr.Slider``). The returned dict ``c`` exposes every key the plugin wires —
    see the module docstring / ``plugin.py`` for the contract.
    """
    c: dict = {}
    use_range = bool(have_rangeslider) and RangeSlider is not None

    gr.Markdown(
        "## 🎞️ Mutator — single-clip video editor\n"
        "Load the clip selected in the gallery, upload one, or receive a "
        "hand-off from another tab, then trim / split on a frame-accurate "
        "mini-timeline, crop, resize, flip, change speed and colour-correct — "
        "with undo/redo. Save in place or as a copy.")

    # -- load row -----------------------------------------------------------
    with gr.Row():
        c["load_btn"] = gr.Button("⟳ Load from preview selection", scale=1)
        c["upload_video"] = gr.Video(label="…or upload a video", scale=1)

    # -- preview ------------------------------------------------------------
    c["preview"] = gr.Video(label="Preview (current working clip)",
                            interactive=False)
    c["info_md"] = gr.Markdown("")

    # -- mini-timeline + trim/split ----------------------------------------
    with gr.Accordion("🎬 Mini-timeline · Trim & Split", open=True):
        c["filmstrip"] = gr.Image(label="Filmstrip", interactive=False,
                                  height=84, show_label=False)

        if use_range:
            c["trim_range"] = RangeSlider(minimum=0, maximum=1, value=(0, 1),
                                          step=1, label="Trim range (frames)")
        else:
            with gr.Row():
                c["trim_start"] = gr.Slider(0, 1, value=0, step=1,
                                            label="Start frame")
                c["trim_end"] = gr.Slider(0, 1, value=1, step=1,
                                          label="End frame")

        c["time_md"] = gr.Markdown("")
        with gr.Row():
            c["start_thumb"] = gr.Image(label="In frame", interactive=False,
                                        height=160)
            c["end_thumb"] = gr.Image(label="Out frame", interactive=False,
                                      height=160)

        c["playhead"] = gr.Slider(0, 1, value=0, step=1,
                                  label="Playhead / split frame")
        with gr.Row():
            c["trim_btn"] = gr.Button("✂ Trim to range", variant="primary")
            c["split_btn"] = gr.Button("⧓ Split at playhead")
        c["keep_radio"] = gr.Radio(["Keep head", "Keep tail"],
                                   value="Keep head",
                                   label="On split, keep")

    # -- transform ----------------------------------------------------------
    with gr.Accordion("🔁 Transform", open=False):
        with gr.Row():
            c["flip_h_btn"] = gr.Button("⇋ Flip horizontal")
            c["flip_v_btn"] = gr.Button("⇵ Flip vertical")

    # -- resize -------------------------------------------------------------
    with gr.Accordion("📐 Resize", open=False):
        with gr.Row():
            c["rs_w"] = gr.Number(label="Width (px)", value=None, precision=0)
            c["rs_h"] = gr.Number(label="Height (px)", value=None, precision=0)
        c["rs_lock"] = gr.Checkbox(label="Lock aspect ratio "
                                         "(blank dim auto-fits)", value=True)
        c["rs_presets"] = gr.Radio(_RESIZE_PRESETS, value="Keep",
                                   label="Presets")
        c["resize_btn"] = gr.Button("Apply resize", variant="primary")

    # -- crop ---------------------------------------------------------------
    with gr.Accordion("✂️ Crop", open=False):
        with gr.Row():
            c["crop_x"] = gr.Number(label="X", value=0, precision=0)
            c["crop_y"] = gr.Number(label="Y", value=0, precision=0)
        with gr.Row():
            c["crop_w"] = gr.Number(label="Width", value=0, precision=0)
            c["crop_h"] = gr.Number(label="Height", value=0, precision=0)
        c["crop_btn"] = gr.Button("Apply crop", variant="primary")

    # -- speed --------------------------------------------------------------
    with gr.Accordion("⏩ Speed", open=False):
        c["speed"] = gr.Slider(0.1, 8.0, value=1.0, step=0.05,
                               label="Playback speed (×)")
        c["speed_btn"] = gr.Button("Apply speed", variant="primary")

    # -- colour -------------------------------------------------------------
    with gr.Accordion("🎨 Colour", open=False):
        c["bri"] = gr.Slider(50, 150, value=100, step=1, label="Brightness")
        c["con"] = gr.Slider(50, 150, value=100, step=1, label="Contrast")
        c["sat"] = gr.Slider(0, 200, value=100, step=1, label="Saturation")
        c["hue"] = gr.Slider(-180, 180, value=0, step=1, label="Hue (°)")
        c["warmth"] = gr.Slider(-100, 100, value=0, step=1, label="Warmth")
        c["gamma"] = gr.Slider(0.5, 2.0, value=1.0, step=0.01, label="Gamma")
        with gr.Row():
            c["color_btn"] = gr.Button("Apply colour", variant="primary")
            c["color_reset_btn"] = gr.Button("Reset sliders")

    # -- history ------------------------------------------------------------
    with gr.Row():
        c["undo_btn"] = gr.Button("↶ Undo", interactive=False)
        c["redo_btn"] = gr.Button("↷ Redo", interactive=False)
        c["reset_btn"] = gr.Button("⟲ Reset to original", interactive=False)

    # -- save ---------------------------------------------------------------
    with gr.Accordion("💾 Save", open=True):
        c["save_name"] = gr.Textbox(
            label="Save-as-copy name",
            placeholder="(defaults to <source>_edited)")
        with gr.Row():
            c["save_inplace_btn"] = gr.Button(
                "💾 Save in place (overwrites original)",
                variant="stop", interactive=False)
            c["save_copy_btn"] = gr.Button("📑 Save as copy",
                                           variant="primary",
                                           interactive=False)
        gr.Markdown(
            "⚠️ *Save in place* permanently overwrites the original source — no "
            "backup. Use *Save as copy* to keep the original. (Uploaded sources "
            "are always saved as a copy.)")

    # -- status -------------------------------------------------------------
    c["status_md"] = gr.Markdown("")

    return c
