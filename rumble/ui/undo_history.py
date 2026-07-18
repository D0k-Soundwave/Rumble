"""Snapshot-based undo/redo history for the profile editor.

Whole-state snapshots (the editor's built ProfileConfig plus the name and
description fields) rather than QUndoCommand pairs: the editor already knows
how to rebuild all of its widgets from a config, so undo/redo is "restore
this snapshot" — no per-widget inverse operations, no first-redo-is-a-no-op
gymnastics. Coalescing of edit bursts is the editor's job (a commit-debounce
timer); this class just refuses no-op commits via dataclass value equality.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rumble.core.apo_config import ProfileConfig


@dataclass
class EditorSnapshot:
    """One point-in-time editor state. The config must be an owned object
    (never aliased to live editor state) — build_config() already returns a
    fresh deep structure."""
    config: ProfileConfig
    name: str = ""
    description: str = ""
    section_idx: int = 0

    def same_state(self, other: "EditorSnapshot | None") -> bool:
        """Value equality of everything that makes an undo step meaningful —
        the selected section is a viewing position, not an edit."""
        return (other is not None
                and self.config == other.config
                and self.name == other.name
                and self.description == other.description)


@dataclass
class UndoHistory:
    """A linear undo/redo stack over EditorSnapshots."""

    MAX_DEPTH = 100

    _undo: list[EditorSnapshot] = field(default_factory=list)
    _redo: list[EditorSnapshot] = field(default_factory=list)
    _current: EditorSnapshot | None = None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def reset(self, baseline: EditorSnapshot) -> None:
        """Start over from *baseline* (a freshly loaded profile)."""
        self._undo.clear()
        self._redo.clear()
        self._current = baseline

    def commit(self, snapshot: EditorSnapshot) -> bool:
        """Record *snapshot* as the new current state. No-op (returns False)
        when nothing meaningful changed; otherwise pushes the previous state
        onto the undo stack and clears the redo branch."""
        if snapshot.same_state(self._current):
            return False
        if self._current is not None:
            self._undo.append(self._current)
            if len(self._undo) > self.MAX_DEPTH:
                del self._undo[0]
        self._redo.clear()
        self._current = snapshot
        return True

    def undo(self) -> EditorSnapshot | None:
        """Step back: returns the snapshot to restore, or None."""
        if not self._undo:
            return None
        if self._current is not None:
            self._redo.append(self._current)
        self._current = self._undo.pop()
        return self._current

    def redo(self) -> EditorSnapshot | None:
        """Step forward again: returns the snapshot to restore, or None."""
        if not self._redo:
            return None
        if self._current is not None:
            self._undo.append(self._current)
        self._current = self._redo.pop()
        return self._current
