"""Native "Send to" panel — Mutator's own embedded sender (vendored from SendTo).

A target dropdown + Send button that routes a value (a frame image, or a clip
path) to any willing receiver discovered via the shared ``plugins/*/sendto.json``
contract, plus the always-on host targets (img2vid init/end, Save to disk). No
``sendto`` package import — :mod:`core.sendout` implements the contract directly —
so the panel works whether or not the SendTo plugin is installed.

    from .ui.sendout import build_send_panel
    build_send_panel(state=self.state, main_tabs=self.main_tabs,
                     image_inputs=[comp], to_path=lambda *v: <path|PIL|None>,
                     refresh_trigger=self.refresh_form_trigger,      # optional
                     get_settings=self.get_current_model_settings)   # optional → img2vid
"""
from __future__ import annotations

import time
import traceback

import gradio as gr

from ..core import sendout as _t


def build_send_panel(state, main_tabs, image_inputs, to_path, *,
                     refresh_trigger=None, get_settings=None,
                     title="📤 Send to", include_host=True, open=False):
    """state / main_tabs / refresh_trigger: host components (request them).
    image_inputs: a Gradio component or list of them, passed as the click inputs.
    to_path(*values) -> a filepath, a PIL/np image (saved automatically), or None.
    get_settings: pass get_current_model_settings to enable the img2vid targets.
    """
    if not isinstance(image_inputs, (list, tuple)):
        image_inputs = [image_inputs]
    image_inputs = list(image_inputs)

    avail = _t.available_targets(include_host=include_host,
                                 include_img2vid=get_settings is not None)
    labels = [t["label"] for t in avail]
    by_label = {t["label"]: t for t in avail}
    default = labels[0] if labels else "Save to disk"

    with gr.Accordion(title, open=open) as box:
        gr.HTML(_t.companion_note_html())
        with gr.Row():
            target = gr.Dropdown(labels, value=default, label="Send to", scale=2)
            send_btn = gr.Button("Send →", variant="primary", scale=1)

    has_trig = refresh_trigger is not None

    def _ret(nav_u, trig_u=None):
        return [nav_u, (trig_u if trig_u is not None else gr.update())] if has_trig else [nav_u]

    def _resolve(vals):
        try:
            img = to_path(*vals)
        except Exception:
            traceback.print_exc()
            return None
        if img is None:
            return None
        if isinstance(img, str):
            return img
        try:
            return _t.save_frame(img, "sent")
        except Exception:
            traceback.print_exc()
            return None

    def _send(tgt_label, state_val, *vals):
        noop = gr.update()
        spec = by_label.get(tgt_label)
        if spec is None:
            gr.Warning("Unknown target."); return _ret(noop)
        path = _resolve(vals)
        if not path:
            gr.Warning("Nothing to send."); return _ret(noop)
        kind = spec["kind"]
        if kind == "save":
            gr.Info(f"Saved to: {path}"); return _ret(noop)
        if kind == "img2vid":
            is_end = spec["slot"] == "end"
            try:
                s = get_settings(state_val)
                s["image_end" if is_end else "image_start"] = [path]
                letter = "E" if is_end else "S"
                ipt = s.get("image_prompt_type") or ""
                if letter not in ipt:
                    s["image_prompt_type"] = (letter + ipt) if ipt else letter
            except Exception:
                traceback.print_exc()
                raise gr.Error("Could not push to the Media Generator.")
            gr.Info("Sent to the Media Generator as the img2vid "
                    f"{'end image' if is_end else 'init'}.")
            return _ret(gr.update(selected="media_gen"), time.time())
        # plugin target (the shared sendto.json contract)
        _t.enqueue(state_val, spec.get("inbox_key"), path,
                   slot=spec.get("slot"), payload=spec.get("payload", "frame"))
        gr.Info(f"Sent to {tgt_label} (loads when you open the tab).")
        nav = gr.update(selected=f"plugin_{spec['tab']}") if spec.get("tab") else noop
        return _ret(nav)

    outs = [main_tabs] + ([refresh_trigger] if has_trig else [])
    send_btn.click(_send, inputs=[target, state] + image_inputs, outputs=outs)
    return {"box": box, "target": target, "send_btn": send_btn}
