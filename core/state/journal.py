"""A phase journal for multi-step operations (098 §3.6).

An operation that changes several things in order (an update, a release train,
a contest draw) writes ``begin`` before its first change, ``phase(name)`` as it
passes each step, and ``done()`` / ``failed(err)`` at the end. A crash between
two steps leaves the journal ``running``; at the next start :meth:`pending`
returns that unfinished operation with its owner, its last phase and the
rollback information that ``begin`` recorded, so the caller can offer resume or
rollback instead of guessing.

The journal is one JSON file, written with :func:`core.state.files.atomic_write_json`
under :func:`core.state.files.file_lock`, so a reader never sees a torn file and
two writers never interleave.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional

from core.state.files import atomic_write_json, file_lock, read_json_strict
from core.state.machine import StateMachine

JOURNAL = StateMachine(
    "state.phase_journal",
    states={"running", "done", "failed"},
    transitions={"running": {"done", "failed"}},
    terminal={"done", "failed"},
    initial={"running"},
)


class JournalBusy(RuntimeError):
    """``begin`` found an unfinished operation that a live caller still owns."""

    def __init__(self, entry: Mapping[str, Any]):
        super().__init__(f"journal busy: {entry.get('op')!r} at phase {entry.get('phase')!r}")
        self.entry = dict(entry)


class PhaseJournal:
    """One operation at a time per journal file."""

    def __init__(self, path: str):
        self.path = str(path)

    # -- reads -----------------------------------------------------------------

    def read(self) -> Optional[Dict[str, Any]]:
        """The entry, or None when there is no journal. An existing journal that
        cannot be read raises ``UnreadableStateFile``: it may hold an unfinished
        operation, so it never reads as "nothing pending" and ``begin`` never
        writes over it."""
        data = read_json_strict(self.path, None)
        return data if isinstance(data, dict) and data.get("op") else None

    def pending(self) -> Optional[Dict[str, Any]]:
        """The unfinished (``running``) operation, or None."""
        entry = self.read()
        if entry and entry.get("state") == "running":
            return entry
        return None

    # -- writes ----------------------------------------------------------------

    def begin(self, op: str, owner: str = "", rollback_info: Optional[Mapping[str, Any]] = None,
              *, force: bool = False) -> Dict[str, Any]:
        """Start ``op``. Refuses (``JournalBusy``) when an operation is still
        ``running`` unless ``force`` — the caller decided that the old one is
        abandoned (it resumed or rolled it back)."""
        with file_lock(self.path):
            cur = self.read()
            if cur and cur.get("state") == "running" and not force:
                raise JournalBusy(cur)
            now = time.time()
            entry = {"op": str(op), "owner": str(owner or ""), "state": "running",
                     "phase": "begin", "phases": ["begin"],
                     "rollback": dict(rollback_info or {}), "error": None,
                     "started_at": now, "updated_at": now}
            atomic_write_json(self.path, entry)
            return entry

    def phase(self, name: str, **info: Any) -> Dict[str, Any]:
        """Record that the operation reached ``name`` (extra ``info`` keys merge
        into the entry's ``info`` map)."""
        def _upd(entry):
            entry["phase"] = str(name)
            entry.setdefault("phases", []).append(str(name))
            if info:
                entry.setdefault("info", {}).update(info)
        return self._update(_upd, None)

    def done(self, phase: Optional[str] = None) -> Dict[str, Any]:
        """End the operation. ``phase`` records its last step in the SAME
        write — two writes left a gap where a kill made a finished operation
        read ``running`` (an applied update was then rolled back at boot)."""
        def _upd(entry):
            if phase is not None:
                entry["phase"] = str(phase)
                entry.setdefault("phases", []).append(str(phase))
        return self._update(_upd, "done")

    def failed(self, err: Any) -> Dict[str, Any]:
        return self._update(lambda e: e.__setitem__("error", str(err)[:2000]), "failed")

    def claim(self, expect_owner: str, new_owner: str) -> Optional[Dict[str, Any]]:
        """Take over an unfinished operation (a compare-and-set on its owner): under
        the lock, the entry must still be ``running`` and still name
        ``expect_owner``. Returns the entry now owned by ``new_owner``, or None
        when another caller took it first or it finished — so two recoverers of
        one crashed operation never both act on it."""
        with file_lock(self.path):
            entry = self.read()
            if (not entry or entry.get("state") != "running"
                    or str(entry.get("owner") or "") != str(expect_owner or "")):
                return None
            entry["owner"] = str(new_owner or "")
            entry["updated_at"] = time.time()
            atomic_write_json(self.path, entry)
            return entry

    def clear(self) -> None:
        """Remove a finished journal (an unfinished one stays: use ``failed``)."""
        import os
        with file_lock(self.path):
            cur = self.read()
            if cur and cur.get("state") == "running":
                raise JournalBusy(cur)
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def _update(self, fn, to: Optional[str]) -> Dict[str, Any]:
        with file_lock(self.path):
            entry = self.read()
            if not entry:
                raise RuntimeError("journal: no operation begun")
            frm = entry.get("state")
            if frm != "running":
                # A phase/done/failed after the op finished is an illegal edge.
                JOURNAL.check(str(frm), to or "running", ctx={"path": self.path})
                return entry
            if to is not None:
                JOURNAL.check(frm, to, ctx={"path": self.path})
                entry["state"] = to
            fn(entry)
            entry["updated_at"] = time.time()
            atomic_write_json(self.path, entry)
            return entry
