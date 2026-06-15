"""The single-track timeline widget: a ``gr.HTML`` mount shell plus the two
hidden ``gr.Textbox`` JSON pipes that bridge the browser timeline
(``assets/static/timeline.js``) to Python.

The JS module itself is delivered via ``WAN2GPPlugin.add_custom_js()`` in
plugin.py (read with :func:`timeline_js`), because Gradio runs ``add_custom_js``
inside its single on-load init function — whereas a ``<script>`` tag inside
``gr.HTML`` innerHTML does NOT execute. The CSS, by contrast, is injected here
as a ``<style>`` blob inside the mount HTML (styles in innerHTML DO apply).

Bridge contract (binding — timeline.js and plugin.py agree to these verbatim):
  * mount ``<div>`` elem_id .......... ``mut_tl_root``     (:data:`TL_ROOT_ID`)
  * JS -> Py hidden Textbox elem_id .. ``mut_tl_to_py``    (:data:`TL_TO_PY_ID`)
  * Py -> JS hidden Textbox elem_id .. ``mut_tl_from_py``  (:data:`TL_FROM_PY_ID`)

The Py -> JS pipe carries an op-envelope ``{"seq": int, "op": "load",
"edit": <edit-json>}`` whose ``.change`` hook runs :data:`APPLY_OP_JS` in the
browser (no server round-trip). ``seq`` is monotonic on the plugin so the JS
can drop stale/replayed loads. See the edit-JSON schema produced by
``core.model.Track.to_json`` / consumed by ``Track.from_json``.

Mirrors the Reel2Reel timeline widget, narrowed to Mutator's single track.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import gradio as gr

logger = logging.getLogger("mutator.timeline")

_STATIC = Path(__file__).resolve().parent.parent / "assets" / "static"
_ASSETS = Path(__file__).resolve().parent.parent / "assets"

# --- bridge elem_ids (verbatim; spec §4.1 / §6.1) --------------------------
TL_ROOT_ID = "mut_tl_root"      # the mount <div> inside the mount gr.HTML
TL_TO_PY_ID = "mut_tl_to_py"    # hidden Textbox: JS -> Py full edit JSON (debounced)
TL_FROM_PY_ID = "mut_tl_from_py"  # hidden Textbox: Py -> JS op-envelope {seq,op,edit}

# JS hook run by tl_from_py.change to apply an op-envelope in the browser.
APPLY_OP_JS = (
    "(p) => { try { window.MutTimeline && window.MutTimeline.applyOp(p); } "
    "catch (e) { console.error('[MUT]', e); } }"
)


def _read(name: str) -> str:
    """Read a static asset, returning ``""`` (and warning) if it is missing.

    Siblings write ``timeline.js`` / ``timeline.css`` into ``assets/static``;
    this factory tolerates their absence so the tab still renders during a
    partial build (the JS guards itself behind ``window.MutTimeline``).
    """
    try:
        return (_STATIC / name).read_text(encoding="utf-8")
    except Exception:
        logger.warning("Could not read assets/static/%s", name, exc_info=True)
        return ""


def timeline_js() -> str:
    """The timeline IIFE — fed to ``add_custom_js`` (the only path that runs)."""
    return _read("timeline.js")


def timeline_css() -> str:
    """The timeline stylesheet — injected as a ``<style>`` blob in the mount."""
    return _read("timeline.css")


def file_url(path) -> str | None:
    """A browser URL for a server-side absolute path via Gradio's static route.

    Output/render filenames contain spaces, so quote (keep ``/`` and ``:``
    literal). Returns ``None`` for falsy input.
    """
    if not path:
        return None
    return "/gradio_api/file=" + quote(str(path), safe="/:")


def register_static_paths(extra_dirs=None) -> None:
    """Allow Gradio to serve our assets + render/thumb/cache dirs by absolute path.

    Cumulative and process-global; safe to call more than once (best-effort).

    Hardened: every dir is normalised to an absolute, resolved path and any that
    resolves to ``/``, a filesystem root, or the user's home directory is
    SKIPPED (with a warning). ``set_static_paths`` is process-global, so an
    over-broad root would make the whole filesystem fetchable through
    ``/gradio_api/file=``.
    """
    home = Path.home().resolve()
    dirs = [str(_ASSETS.resolve())]
    for d in (extra_dirs or []):
        if not d:
            continue
        try:
            rp = Path(str(d)).expanduser().resolve()
        except Exception:
            continue
        if rp == rp.parent or rp == home:        # filesystem root or $HOME
            logger.warning("Refusing to register over-broad static path %s", rp)
            continue
        dirs.append(str(rp))
    try:
        gr.set_static_paths(dirs)
    except Exception:
        logger.debug("set_static_paths unavailable", exc_info=True)


def build_timeline_widget() -> dict:
    """Build the timeline mount + the two hidden bridge textboxes.

    Returns ``{"tl_mount", "tl_to_py", "tl_from_py"}``; plugin.py owns the
    wiring (the Py -> JS ``.change(js=APPLY_OP_JS)`` hook and the JS -> Py
    debounced ``.change`` handler). The mount HTML carries the timeline CSS as
    an inline ``<style>`` blob and the empty ``#mut_tl_root`` div that
    ``timeline.js`` mounts into (with a "Loading…" placeholder until then).
    """
    css = timeline_css()
    mount_html = (
        f"<style>{css}</style>"
        f"<div id='{TL_ROOT_ID}'>"
        f"<div class='mut-tl'><div class='mut-scroll' style='padding:18px;color:#888'>"
        f"Loading timeline…</div></div>"
        f"</div>"
    )
    c: dict = {}
    c["tl_mount"] = gr.HTML(mount_html)
    # Hidden JSON bridges. Kept interactive so the browser can change them and
    # Python can write back through them.
    c["tl_to_py"] = gr.Textbox(
        elem_id=TL_TO_PY_ID, visible=False, interactive=True, value="", lines=1
    )
    c["tl_from_py"] = gr.Textbox(
        elem_id=TL_FROM_PY_ID, visible=False, interactive=True, value="", lines=1
    )
    return c
