"""Filesystem paths and save semantics for Mutator.

Mutator edits a single clip non-destructively: every edit op writes a NEW
working file into the plugin's cache dir (so Gradio's content-hash refreshes the
preview), and a final Save either copies the working file into the host's
outputs dir or overwrites the original source in place.

Ported from Trimline's ``core/paths.py`` (collision-safe ``name(2).ext`` naming
to mirror Wan2GP's ``get_available_filename``), adapted for Mutator's cache and
working-file model. No Gradio/host imports — unit-testable. Dependency-light:
pathlib, shutil, uuid, os, time only.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

# Known video extensions — used to decide whether a trailing ".xxx" on a
# user-typed save name is a real extension to strip or part of the name.
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif"}


def plugin_root() -> Path:
    """Absolute path to the plugin root (the dir containing ``core/``)."""
    return Path(__file__).resolve().parent.parent


def cache_dir() -> Path:
    """Scratch dir for working files, filmstrips and thumbnails.

    Lives under the plugin root as ``.mutator_cache`` and is created on demand.
    """
    d = plugin_root() / ".mutator_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_path(suffix: str = ".mp4") -> str:
    """Unique working-file path inside :func:`cache_dir`.

    Each edit writes a brand-new file (``work_<uuid>.<ext>``) so Gradio's
    content hash changes and the preview refreshes.
    """
    return str(cache_dir() / f"work_{uuid4().hex}{suffix}")


def outputs_dir(save_path: str | None = None) -> str:
    """Absolute, existing outputs dir from the host's ``save_path`` global.

    Defaults to ``"outputs"`` (relative to the Wan2GP root = cwd) when no
    ``save_path`` is supplied, matching the host's convention.
    """
    out = save_path or "outputs"
    if not os.path.isabs(out):
        out = os.path.join(os.getcwd(), out)
    os.makedirs(out, exist_ok=True)
    return out


def available_filename(target_dir: str, basename: str, suffix: str = "",
                       ext: str = ".mp4", strip_ext: bool = True) -> str:
    """Collision-safe ``<stem><suffix><ext>`` in *target_dir*, falling back to
    ``<stem><suffix>(2)<ext>`` etc. Ported from ``wgp.get_available_filename``.

    ``strip_ext`` splits a trailing extension off *basename* (right for real file
    paths); pass ``False`` to treat *basename* as a literal stem (right for a
    free-text user name, where ``v1.2`` must not become ``v1``).
    """
    if strip_ext:
        stem, _ = os.path.splitext(os.path.basename(basename))
    else:
        stem = os.path.basename(basename)
    stem = stem + suffix
    full = os.path.join(target_dir, f"{stem}{ext}")
    if not os.path.exists(full):
        return full
    counter = 2
    while True:
        full = os.path.join(target_dir, f"{stem}({counter}){ext}")
        if not os.path.exists(full):
            return full
        counter += 1


def save_as_copy(src: str, original: str | None,
                 name: str | None, save_path: str | None) -> str:
    """Copy the working file into the outputs dir as a NEW file.

    Uses *name* when given, else ``<original-stem>_edited``. Never overwrites —
    collisions get ``(2)``, ``(3)``, … Returns the destination path.
    """
    out_dir = outputs_dir(save_path)
    if (name or "").strip():
        # Keep the full typed name as the stem unless it ends in a real video
        # extension (so 'v1.2' -> 'v1.2.mp4', but 'clip.mp4' -> 'clip.mp4', not
        # 'clip.mp4.mp4'). available_filename's splitext is right for source
        # PATHS (real extensions), wrong for free-text names — so resolve here.
        typed = name.strip()
        stem, ext = os.path.splitext(os.path.basename(typed))
        base = stem if ext.lower() in _VIDEO_EXTS else os.path.basename(typed)
        dest = available_filename(out_dir, base, "", ".mp4", strip_ext=False)
    else:
        dest = available_filename(out_dir, original or "video", "_edited", ".mp4")
    shutil.copy2(src, dest)
    return dest


def save_in_place(src: str, dest: str) -> None:
    """Overwrite *dest* with the bytes of *src* (destructive, atomic-ish).

    Used for gallery-loaded clips, where *dest* is the canonical original.
    Copies to a sibling temp file first, then ``os.replace`` swaps it in so a
    crash mid-write cannot leave *dest* truncated. Uploads have no canonical
    original and route to :func:`save_as_copy` instead.
    """
    dest_path = Path(dest)
    tmp = dest_path.with_name(f".{dest_path.name}.{uuid4().hex}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest_path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def prune_cache(max_age_hours: float = 24.0) -> None:
    """Best-effort cleanup of stale working files from :func:`cache_dir`.

    Every edit writes a fresh ``work_<uuid>.mp4`` (and a split discards one half),
    plus filmstrips/thumbnails, so the cache grows over a long session and
    persists across restarts. Called once on plugin load to reclaim anything
    older than *max_age_hours*. Never raises — the cache is disposable.
    """
    try:
        cutoff = time.time() - max_age_hours * 3600.0
        for p in cache_dir().iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass
