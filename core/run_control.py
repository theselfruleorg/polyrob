"""One background run: list, pause, resume, stop — the owner ``/run`` verb.

The ONE implementation behind Telegram ``/run`` and the terminal ``/run``
(070 decision 1, 2026-10-01: Telegram must not be the one seat that cannot stop
one run). Rows come from :func:`core.status_live.build_live_status` (the live
sessions the registry can prove, for THIS tenant); control goes through the
durable per-session mailbox :class:`core.session_control.SessionControl`, which
the agent acknowledges at its next step boundary. Nothing here changes a goal's
or a job's own state: a stopped goal run is retried or not by the board's own
rules.

⚠️ A paused goal or scheduled job still runs inside its time limit
(``agents/task/goals/dispatcher.py`` / ``cron/scheduler.py`` wrap the run in
``asyncio.wait_for``); a long pause can end the run. The pause reply says so.
"""
from __future__ import annotations

import os
from typing import Iterable, List, Optional, Tuple

#: Seconds the reply waits for the step-boundary acknowledgement.
ACK_WAIT_S = 2.0

USAGE = "Usage: /run [pause|resume|stop <n>]"
NOTHING_RUNS = "Nothing runs in the background now."
UNREADABLE = "I could not read which runs are live just now. Nothing was changed."

_WORDS = {"pause": "pause", "resume": "resume", "stop": "cancel"}
_DONE = {"paused": "Paused", "running": "Running again", "cancelled": "Stopped"}
_KIND_WORD = {"goal": "goal", "job": "scheduled job", "helper": "helper", "chat": "chat"}


def _kind(creator: Optional[str], is_goal: bool) -> str:
    c = (creator or "").lower()
    if is_goal or c == "goal":
        return "goal"
    if c == "cron":
        return "job"
    if "deleg" in c or "helper" in c or "sub" in c:
        return "helper"
    return "chat"


def list_runs(user_id: str, data_dir: str, sessions_root: str, *,
              exclude: Iterable[str] = ()) -> Tuple[List[dict], bool]:
    """The live runs of *user_id*, oldest first, minus *exclude* (the asking
    chat). ``readable`` is False when the session registry could not be read —
    an empty list then proves nothing."""
    from core.status_live import build_live_status

    live = build_live_status(user_id, data_dir, sessions_root)
    sessions = live.get("sessions")
    if sessions is None:
        return [], False
    goal_titles = {g.get("session_id"): g.get("title")
                   for g in (live.get("goals") or []) if g.get("session_id")}
    skip = {s for s in exclude if s}
    rows = []
    for s in sessions:
        sid = s.get("session_id")
        if not sid or sid in skip:
            continue
        task_lines = (s.get("task") or "").splitlines()
        title = goal_titles.get(sid) or (task_lines[0] if task_lines else "")
        rows.append({"session_id": sid,
                     "kind": _kind(s.get("creator"), sid in goal_titles),
                     "title": (title or "untitled").strip()[:80],
                     "since": s.get("since") or 0.0,
                     "dir": os.path.join(sessions_root, user_id, sid)})
    rows.sort(key=lambda r: (r["since"], r["session_id"]))
    return rows, True


def _state_word(directory: str) -> str:
    from core.session_control import SessionControl
    try:
        row = SessionControl(directory).read()
    except Exception:
        return ""
    if not row or not SessionControl.alive(row):
        return ""
    return " (paused)" if row.get("acknowledged") == "paused" else ""


def _pick(rows: List[dict], token: str) -> Optional[dict]:
    if token.isdigit():
        n = int(token)
        return rows[n - 1] if 1 <= n <= len(rows) else None
    if len(token) >= 6:
        hits = [r for r in rows if r["session_id"].startswith(token)]
        return hits[0] if len(hits) == 1 else None
    return None


async def run_reply(user_id: str, data_dir: str, sessions_root: str,
                    args: List[str], *, exclude: Iterable[str] = ()) -> str:
    """The whole ``/run`` answer as plain text (both seats render it as is)."""
    from core.session_control import SessionControl

    if args and (len(args) != 2 or args[0].lower() not in _WORDS):
        return USAGE
    try:
        rows, readable = list_runs(user_id, data_dir, sessions_root, exclude=exclude)
    except Exception:
        return UNREADABLE
    if not readable:
        return UNREADABLE
    if not args:
        if not rows:
            return NOTHING_RUNS
        lines = ["Runs in the background now:"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r['title']} — {_KIND_WORD[r['kind']]}{_state_word(r['dir'])}")
        lines.append("Pause, resume or stop one: /run pause 1, /run resume 1, "
                     f"/run stop {len(rows)}")
        return "\n".join(lines)

    word, token = args[0].lower(), args[1]
    row = _pick(rows, token)
    if row is None:
        return (f"There is no run {token} now. Send /run to see the list again."
                if rows else NOTHING_RUNS)
    label = f"{row['title']} ({_KIND_WORD[row['kind']]})"
    try:
        state = await SessionControl(row["dir"]).request_and_wait(_WORDS[word],
                                                                  timeout=ACK_WAIT_S)
    except Exception:
        return f"I could not reach {label}. Nothing was changed."
    if state is None:
        return (f"{label} cannot be controlled from here: no live run holds it. "
                "Nothing was changed.")
    if state == "pending":
        return f"{label}: {word} requested. Rob does it at the next step."
    out = f"{_DONE[state]}: {label}."
    if state == "paused" and row["kind"] in ("goal", "job"):
        out += " Its time limit keeps counting, so a long pause can end the run."
    return out
