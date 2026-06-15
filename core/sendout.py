"""Native SendTo target logic — Mutator's own copy of the shared hand-off
contract so the editor needs NO ``sendto`` package installed to route results.

The SendTo *contract* is decoupled by design: receivers advertise themselves in a
``plugins/*/sendto.json`` manifest and drain their own ``state[inbox_key]`` on tab
entry. Reading that manifest + writing the shared session ``state`` is plain
stdlib — no cross-plugin import — so Mutator implements it directly here and still
interoperates with every other OrphanSuite plugin (Reel2Reel, Image Suite, …) and
the SendTo plugin itself, installed or not.

Pure stdlib (+ Pillow only inside :func:`save_frame`); no Gradio here, so it is
safe to import anywhere. The Gradio "Send to" panel lives in :mod:`ui.sendout`.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import traceback

from . import paths

MANIFEST = "sendto.json"

# Built-in host targets — always available, need no receiver plugin. ``accepts``
# is the media kinds a target can receive ("image" / "video").
BUILTIN_TARGETS = [
    {"label": "img2vid (init)",      "kind": "img2vid", "tab": None, "slot": "start", "accepts": ["image"]},
    {"label": "img2vid (end image)", "kind": "img2vid", "tab": None, "slot": "end",   "accepts": ["image"]},
    {"label": "Save to disk",        "kind": "save",    "tab": None, "slot": None,    "accepts": ["image", "video"]},
]

# OrphanSuite companions advertised in the panel for discovery. ``repo`` is None
# for plugins without a public release yet (greyed, no dead link).
COMPANIONS = [
    {"name": "Image Suite",       "plugin_dir": "ImageSuite-Wan2GP",        "targets": "Img2Img · MultiCanvas · Modify", "repo": "https://github.com/saintorphan/ImageSuite-Wan2GP"},
    {"name": "Reel2Reel",         "plugin_dir": "Reel2Reel-Wan2GP",         "targets": "timeline",                       "repo": "https://github.com/saintorphan/Reel2Reel-Wan2GP"},
    {"name": "Send To",           "plugin_dir": "SendTo-Wan2GP",            "targets": "frame router",                   "repo": "https://github.com/saintorphan/SendTo-Wan2GP"},
]


def plugins_dir() -> str:
    """The host runs from the repo root and inserts ``plugins`` at sys.path[0]."""
    cand = os.path.abspath("plugins")
    if os.path.isdir(cand):
        return cand
    for p in sys.path:
        if os.path.basename(os.path.normpath(p)) == "plugins" and os.path.isdir(p):
            return p
    return cand


def scan_targets() -> list[dict]:
    """Discover receiver targets from every ``plugins/*/sendto.json``. Invalid
    manifests are skipped so one bad plugin can't break discovery."""
    out = []
    for mf in sorted(glob.glob(os.path.join(plugins_dir(), "*", MANIFEST))):
        try:
            with open(mf, encoding="utf-8") as fh:
                m = json.load(fh)
        except Exception:
            traceback.print_exc()
            continue
        tab, key, payload = m.get("tab"), m.get("inbox_key"), m.get("payload", "frame")
        accepts = m.get("accepts") or ["image"]
        if not key or not isinstance(m.get("targets"), list):
            continue
        for t in m["targets"]:
            label = (t or {}).get("label")
            if not label:
                continue
            out.append({"label": label, "kind": "plugin", "tab": tab,
                        "inbox_key": key, "payload": payload, "slot": t.get("slot"),
                        "accepts": (t.get("accepts") or accepts)})
    return out


def available_targets(include_host: bool = True, include_img2vid: bool = True,
                      include_save: bool = True, exclude_tab=None,
                      accepts=None) -> list[dict]:
    """Scanned plugin targets first, then the always-on host targets.

    - ``include_host`` adds img2vid + Save-to-disk.
    - ``include_img2vid=False`` drops img2vid (needs get_current_model_settings).
    - ``include_save=False`` drops Save-to-disk.
    - ``exclude_tab`` drops a plugin's OWN targets (a same-tab inbox hand-off
      can't fire — Mutator excludes itself).
    - ``accepts``: a media kind ("image"/"video") — keep only matching targets.
    """
    rows = [t for t in scan_targets()
            if not (exclude_tab and t.get("tab") == exclude_tab)]
    if include_host:
        for t in BUILTIN_TARGETS:
            if t["kind"] == "img2vid" and not include_img2vid:
                continue
            if t["kind"] == "save" and not include_save:
                continue
            rows.append(t)
    if accepts:
        rows = [t for t in rows if accepts in (t.get("accepts") or ["image"])]
    seen, out = set(), []
    for t in rows:
        if t["label"] in seen:
            continue
        seen.add(t["label"])
        out.append(t)
    return out


def enqueue(state, inbox_key, path, slot=None, payload="frame") -> None:
    """Append a hand-off to a receiver's inbox on the shared session state. The
    receiver drains ``state[inbox_key]`` in ``on_tab_select``. ``payload`` "path"
    appends a bare path string (list inboxes, e.g. Reel2Reel / Mutator); "frame"
    appends ``{path, slot}``."""
    if not isinstance(state, dict) or not inbox_key or not path:
        return
    box = list(state.get(inbox_key) or [])
    box.append(str(path) if payload == "path" else {"path": str(path), "slot": slot})
    state[inbox_key] = box[-500:]


def save_frame(img, tag: str = "frame") -> str:
    """Persist a PIL/ndarray image into Mutator's own cache and return its path.

    Writing under :func:`core.paths.cache_dir` (rather than a system temp dir)
    keeps the handed-off frame inside the plugin's registered static-serve roots,
    so the host can read it back out for a receiver."""
    from PIL import Image
    if not isinstance(img, Image.Image):
        img = Image.fromarray(img)
    p = str(paths.cache_dir() / f"{tag}_{int(time.time() * 1000)}.png")
    img.convert("RGB").save(p)
    return p


def companion_note_html() -> str:
    """A 'works with' line advertising the OrphanSuite companions — installed ones
    active, missing ones greyed (with an install link when the plugin is public)."""
    pdir = plugins_dir()
    chips = []
    for c in COMPANIONS:
        if os.path.isdir(os.path.join(pdir, c["plugin_dir"])):
            chips.append(f'<span style="color:#6cc070">✓ {c["name"]}</span>'
                         f'<span style="opacity:.55"> — {c["targets"]}</span>')
        else:
            tail = (f' · <a href="{c["repo"]}" target="_blank" rel="noopener">install</a>'
                    if c.get("repo") else ' · <span style="opacity:.5">(not yet public)</span>')
            chips.append(f'<span style="opacity:.4">{c["name"]} — {c["targets"]}</span>{tail}')
    return ('<div style="font-size:.85em;line-height:1.6;margin:2px 0 4px">'
            'Works with the <b>OrphanSuite</b> plugins — installed ones appear as '
            'targets above:<br>' + '&nbsp;&nbsp;·&nbsp;&nbsp;'.join(chips) + '</div>')
