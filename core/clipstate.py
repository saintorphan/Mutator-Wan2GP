"""In-memory edit session for Mutator's single-clip video editor.

Holds the working clip plus undo/redo history on the plugin instance (the host
keeps ``self._clip = ClipSession()``, mirroring how Reel2Reel keeps
``self._project``). Each edit op probes a freshly rendered working file, wraps it
in a :class:`ClipInfo`, and pushes it onto the undo stack via
:meth:`ClipSession.push`.

Pure Python — no Gradio, no ffmpeg, no host imports — so it is unit-testable on
its own. The ``origin`` (the real source path to overwrite on Save-in-place) and
``is_upload`` flags are carried forward across edits so a Save-in-place always
targets the original file no matter how many edits were applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# Cap on the undo history; older states are dropped from the bottom.
_UNDO_CAP = 30


@dataclass
class ClipInfo:
    """Snapshot describing one state of the clip being edited.

    ``path`` is the *current working file* on disk (a fresh file per edit so
    Gradio's content-hash refreshes the preview). ``origin`` is the real source
    path to overwrite on Save-in-place — ``None`` for uploads and any clip that
    has no on-disk source to write back to.
    """

    path: str
    fps: float
    num_frames: int
    duration: float
    width: int
    height: int
    has_audio: bool = False
    fps_inexact: bool = False
    is_upload: bool = False
    origin: str | None = None


class ClipSession:
    """The clip currently open in the editor plus its undo/redo history.

    ``current`` is the live :class:`ClipInfo` (or ``None`` when nothing is
    loaded). ``_undo`` holds prior states (oldest first, capped at
    :data:`_UNDO_CAP`); ``_redo`` holds states undone but not yet superseded.
    ``_origin_info`` remembers the freshly loaded state so :meth:`reset` can
    return to it.
    """

    def __init__(self) -> None:
        self.current: ClipInfo | None = None
        self._undo: list[ClipInfo] = []
        self._redo: list[ClipInfo] = []
        self._origin_info: ClipInfo | None = None

    # --- loading ------------------------------------------------------------

    def load(self, info: ClipInfo) -> None:
        """Open ``info`` as a fresh clip, discarding all history."""
        self.current = info
        self._origin_info = info
        self._undo.clear()
        self._redo.clear()

    # --- editing ------------------------------------------------------------

    def push(self, info: ClipInfo) -> None:
        """Commit ``info`` as the new current state.

        The previous state moves onto the undo stack and the redo stack is
        cleared (a new edit forks history). ``origin`` and ``is_upload`` are
        carried forward from the previous current — edits keep the same source
        so Save-in-place still targets the real file — and the undo stack is
        capped at :data:`_UNDO_CAP`.
        """
        prev = self.current
        if prev is not None:
            info = replace(info, origin=prev.origin, is_upload=prev.is_upload)
            self._undo.append(prev)
            if len(self._undo) > _UNDO_CAP:
                # Drop the oldest states beyond the cap.
                del self._undo[: len(self._undo) - _UNDO_CAP]
        self.current = info
        self._redo.clear()

    # --- history queries ----------------------------------------------------

    def can_undo(self) -> bool:
        """True when there is a prior state to step back to."""
        return bool(self._undo)

    def can_redo(self) -> bool:
        """True when there is an undone state to step forward to."""
        return bool(self._redo)

    # --- history navigation -------------------------------------------------

    def undo(self) -> ClipInfo | None:
        """Step back one state. Returns the new current (or ``None`` if empty)."""
        if not self._undo:
            return self.current
        if self.current is not None:
            self._redo.append(self.current)
        self.current = self._undo.pop()
        return self.current

    def redo(self) -> ClipInfo | None:
        """Step forward one state. Returns the new current (or ``None``)."""
        if not self._redo:
            return self.current
        if self.current is not None:
            self._undo.append(self.current)
        self.current = self._redo.pop()
        return self.current

    def reset(self) -> ClipInfo | None:
        """Return to the freshly loaded state, recording the jump as an undo."""
        if self._origin_info is None:
            return self.current
        if self.current is not None and self.current is not self._origin_info:
            self._undo.append(self.current)
            if len(self._undo) > _UNDO_CAP:
                del self._undo[: len(self._undo) - _UNDO_CAP]
        self.current = self._origin_info
        self._redo.clear()
        return self.current
