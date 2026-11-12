"""An owner verb leaves a record (116 W3.7).

`/status` at 18:01Z left no row and no log line: the reply of a verb outside
the money set was sent and forgotten, so "what did the bot tell me" had no
answer. Two records, both small:

* ONE ``owner_command`` event per verb: ``{verb, seat, outcome, reply_chars}``.
  No arguments and no body — a verb line can carry an address or an amount,
  and the event log is read by status and digests.
* The LAST rendered ``/status`` text per seat (one entry, overwritten), in
  ``<data home>/owner_status_last.json``. It is NOT written to the owner
  thread: that store is injected into the model's context.

Everything here is fail-open: a record that cannot be written never costs the
owner the reply.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

LAST_STATUS_FILE = "owner_status_last.json"
#: A status text is a few KB; a larger one is cut (the file is a note, not a log).
_LAST_STATUS_MAX_CHARS = 16000


def _outcome(text: str) -> str:
    try:
        from core.surfaces import cards
        if cards.refused_quote(text):
            return OUTCOME_REFUSED
        if cards.failed_result(text):
            return OUTCOME_FAILED
    except Exception:
        logger.debug("owner verb outcome not classified", exc_info=True)
    return OUTCOME_OK


def record_owner_command(user_id: str, verb: str, seat: str, reply_text: str = "", *,
                         failed: bool = False, session_id: str = "",
                         data_dir: Optional[str] = None) -> None:
    """One ``owner_command`` event; for ``/status`` also the last text per seat."""
    try:
        from core.event_kinds import OWNER_COMMAND
        from core.event_log import emit
        text = str(reply_text or "")
        verb = str(verb or "").split()[0][:40] if verb else "?"
        emit(OWNER_COMMAND, source="owner_verb", user_id=user_id, session_id=session_id,
             attrs={"verb": verb, "seat": str(seat or "?"),
                    "outcome": OUTCOME_FAILED if failed else _outcome(text),
                    "reply_chars": len(text)})
        if verb == "/status" and text and not failed:
            save_last_status(user_id, seat, text, data_dir=data_dir)
    except Exception:
        logger.warning("owner verb %s: record skipped", verb, exc_info=True)


def _last_status_path(data_dir: Optional[str]) -> str:
    from core.runtime_paths import data_dir_or_home
    return os.path.join(data_dir_or_home(data_dir), LAST_STATUS_FILE)


def save_last_status(user_id: str, seat: str, text: str, *,
                     data_dir: Optional[str] = None) -> None:
    path = _last_status_path(data_dir)
    try:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[f"{user_id}|{seat}"] = {"ts": time.time(), "seat": str(seat),
                                    "text": str(text)[:_LAST_STATUS_MAX_CHARS]}
        from core.data_perms import apply_birth_mode
        from core.state.files import atomic_write_json
        atomic_write_json(path, data)
        apply_birth_mode(path)
    except Exception:
        logger.warning("last status text not saved (%s)", path, exc_info=True)


def last_status(user_id: str, *, data_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{seat: {"ts", "text"}}`` — the last ``/status`` text each seat sent this
    owner. ``{}`` when none was saved. Raises on a file that cannot be read."""
    path = _last_status_path(data_dir)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out: Dict[str, Dict[str, Any]] = {}
    for key, row in (data or {}).items():
        uid, _, seat = str(key).partition("|")
        if uid == str(user_id) and isinstance(row, dict):
            out[seat] = {"ts": row.get("ts"), "text": row.get("text") or ""}
    return out


def last_command_ts(user_id: str, verb: str, *, data_dir: Optional[str] = None) -> Optional[float]:
    """When the owner last ran *verb* (any seat), or None. Raises on a failed
    read; an absent log is None."""
    from core.event_kinds import OWNER_COMMAND
    from core.event_log import open_event_log
    log = open_event_log(data_dir)
    if log is None:
        return None
    row = log._run(
        "SELECT MAX(ts) AS ts FROM telemetry_events WHERE kind = ? AND user_id = ? "
        "AND json_extract(attrs, '$.verb') = ? AND json_extract(attrs, '$.outcome') = ?",
        (OWNER_COMMAND, str(user_id), str(verb), OUTCOME_OK), fetch="one")
    return float(row["ts"]) if row and row["ts"] is not None else None
