"""A list preference changes by ONE entry, under the file lock (125 S20).

Every caller used to read the list outside the lock, append, and write the whole list
back: two answers of "[n]ever" at once lost one ``approvals.deny`` entry, and the
refused action could run again. Here the read, the change and the write are one step
(:func:`core.prefs._locked_list` over :func:`core.prefs_io.locked_update`). A stored
list this version cannot validate refuses the change and stays as it is.

A queued widening or narrowing is ``propose_pref_change(op="add_entry"|"remove_entry")``:
it stores the ENTRY, and the promote recomputes against the list in force then.

Imports :mod:`core.prefs`; ``core.prefs`` never imports this module back (the cycle
ratchet).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from core import prefs as _prefs


def add_list_entries(home_dir: Path | str, user_id: Optional[str], key: str,
                     entries: Iterable[object],
                     instance_id: Optional[str] = None) -> tuple[bool, str, list]:
    """Add *entries* to the list *key*: ``(ok, err, added)``. *added* holds only the
    entries that were not there yet — an entry already present is no error."""
    spec = _prefs.PREF_SCHEMA.get(key)
    if spec is None or spec.type != "list":
        return False, (_prefs.unknown_pref_error(key) if spec is None
                       else f"{key} is not a list preference"), []
    ok, new, err = _prefs.validate_pref(key, list(entries or []))
    if ok and not new:
        ok, err = False, f"{key}: no entry to add"
    if not ok:
        return False, err, []
    ok, err, before, after = _prefs._locked_list(
        home_dir, user_id, key,
        lambda cur: cur + [e for i, e in enumerate(new) if e not in cur and e not in new[:i]],
        instance_id)
    return ok, err, [e for e in after if e not in before]

