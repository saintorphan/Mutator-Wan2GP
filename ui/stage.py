"""The STAGE widget: a real video-preview player (transport controls + a crop
overlay) synced bidirectionally to the timeline playhead.

Unlike the v0.3 crop iframe, the stage JS is a PARENT-DOCUMENT module shipped via
``WAN2GPPlugin.add_custom_js()`` (exactly like ``timeline.js``) and mounts into
the ``#mut_stage_root`` div. Living in the parent document lets ``stage.js`` and
``timeline.js`` share ``window`` and sync directly
(``window.MutStage`` <-> ``window.MutTimeline``) with no cross-iframe calls.

Bridge contract (binding — ``stage.js`` / ``timeline.js`` / ``plugin.py`` agree):

  * mount ``<div>`` elem_id ........... ``mut_stage_root``    (:data:`STAGE_ROOT_ID`)
  * Py -> JS clip injector (gr.HTML) .. ``mut_stage_from_py`` (:data:`STAGE_FROM_PY_ID`)
  * JS -> Py crop rect (gr.Textbox) ... ``mut_crop_to_py``    (:data:`CROP_TO_PY_ID`)

The Py -> JS pipe carries a one-shot ``<script>`` that calls
``window.MutStage.loadClip({...})`` with the selected clip's VIDEO URL + clip
params (in/out/speed/reverse/src_w/src_h/crop). A ``nonce`` comment forces the
gr.HTML value to differ on every push so Gradio re-renders and the injector
re-executes. The crop rect emitted back (JS -> Py) is unchanged from v0.3:
``{seg_id,x,y,w,h}`` integers in SOURCE pixels, even-rounded in Python.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import gradio as gr

logger = logging.getLogger("mutator.stage")

_STATIC = Path(__file__).resolve().parent.parent / "assets" / "static"

# --- bridge elem_ids (verbatim; shared with stage.js / timeline.js / plugin) ---
STAGE_ROOT_ID = "mut_stage_root"        # the mount <div> the stage JS mounts into
STAGE_FROM_PY_ID = "mut_stage_from_py"  # hidden gr.HTML: Py -> JS one-shot loadClip injector
CROP_TO_PY_ID = "mut_crop_to_py"        # hidden gr.Textbox: JS -> Py {seg_id,x,y,w,h} (source px)


def _read(name: str) -> str:
    """Read a static asset, returning ``""`` (and warning) if it is missing.

    The shipped ``stage.js`` / ``stage.css`` live in ``assets/static``; this
    factory tolerates their absence so the tab still renders during a partial
    build (the JS guards itself behind ``window.MutStage``)."""
    try:
        return (_STATIC / name).read_text(encoding="utf-8")
    except Exception:
        logger.warning("Could not read assets/static/%s", name, exc_info=True)
        return ""


def stage_js() -> str:
    """The stage IIFE — fed to ``add_custom_js`` (the only path that runs)."""
    return _read("stage.js")


def stage_css() -> str:
    """The stage stylesheet — injected as a ``<style>`` blob in the mount HTML."""
    return _read("stage.css")


def build_stage_widget() -> dict:
    """Build the stage mount + its bridge components.

    Returns ``{"stage_mount", "crop_to_py", "stage_from_py"}``; ``plugin.py`` owns
    the wiring (the JS -> Py crop ``.change`` handler; the Py -> JS clip injector
    is written into ``stage_from_py`` as part of the LOAD_OUTS refresh). The mount
    HTML carries the stage CSS as an inline ``<style>`` blob and the empty
    ``#mut_stage_root`` div the stage JS mounts into (with a placeholder)."""
    css = stage_css()
    mount_html = (
        f"<style>{css}</style>"
        f"<div id='{STAGE_ROOT_ID}'>"
        f"<div class='mut-stage'><div class='mut-stage-disp'>"
        f"<div class='mut-stage-empty' style='position:static;padding:40px'>"
        f"Loading preview…</div></div></div>"
        f"</div>"
    )
    c: dict = {}
    c["stage_mount"] = gr.HTML(mount_html)
    # Hidden JS -> Py crop pipe; interactive so the stage can write into it.
    c["crop_to_py"] = gr.Textbox(
        elem_id=CROP_TO_PY_ID, visible=False, interactive=True, value="", lines=1
    )
    # Hidden Py -> JS one-shot loadClip injector carrier; its value is set to
    # stage_clip_html(...) by the LOAD_OUTS refresh.
    c["stage_from_py"] = gr.HTML(visible=False, elem_id=STAGE_FROM_PY_ID)
    return c


def _js_value(v) -> str:
    """JSON-encode a value for embedding inside an inline ``<script>``, neutralising
    the chars that could prematurely close the ``<script>`` element."""
    return (
        json.dumps(v)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def stage_clip_html(payload: dict, nonce: str = "") -> str:
    """Build the hidden one-shot injector (Python -> JS) that loads a clip onto the
    stage.

    The returned HTML is assigned to the ``stage_from_py`` ``gr.HTML``. Its inline
    ``<script>`` calls ``window.MutStage.loadClip(<payload>)`` where ``payload`` is
    ::

        { "url": "/gradio_api/file=<src>", "seg_id": "s1", "in": 0.0, "out": 4.2,
          "speed": 1.0, "reverse": false, "src_w": 1920, "src_h": 1080,
          "crop": {"x":0,"y":0,"w":1920,"h":1080} | null }

    The ``nonce`` comment forces the gr.HTML value to differ on every call so
    Gradio re-renders and the injector re-executes even when the same clip is
    re-pushed. The whole payload is JSON-escaped so it can't break out of the
    ``<script>``."""
    data = _js_value(payload or {})
    return (
        "<div style='display:none'><script>/*" + str(nonce) + "*/"
        "try{window.MutStage&&window.MutStage.loadClip(" + data + ");}catch(e){}"
        "</script></div>"
    )
