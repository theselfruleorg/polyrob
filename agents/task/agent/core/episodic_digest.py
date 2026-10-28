"""Session-start activity digest (2026-07-03).

Passive, read-only, pinned foundation message that puts the agent's OWN recent runs
in front of it — so it can answer 'what did I run in the last 8h?' without a tool call.
Scoped to owner/chat sessions ONLY (never goal/cron/sub-agent) to avoid a per-run token
tax + distraction. Fail-open.

NOTE on `kind not in ("chat",)`: goal/cron sessions are excluded by the CALLER (the
first-step wiring in `memory_prefetch.py::MemoryPrefetchMixin._maybe_inject_episodic_digest`
resolves `kind` via `core.autonomy_marker.is_autonomous(session_id)` and only
ever passes `kind="chat"` here after confirming the session is not autonomous — see that
module's docstring for why this can't be decided at Agent-construction time). This function
re-checks `kind` itself so it is also safe to call directly (as the tests below do) with an
explicit non-chat `kind`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def build_activity_digest(*, user_id: Optional[str], kind: str,
                                is_sub_agent: bool, window_hours: int = 24,
                                limit: int = 10):
    try:
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.episodic_digest_inject():
            return None
        if is_sub_agent or kind not in ("chat",):        # owner/chat sessions only
            return None
        from modules.memory.registry import memory_recall_episodes
        since_ts = int(time.time()) - window_hours * 3600
        # exclude_surfaced=True: an episode already delivered out-of-band (cron
        # email/telegram/twitter, or a goal self-wake) doesn't need to be
        # re-surfaced here too — recent_activity() (explicit query) still sees
        # everything regardless of the surfaced flag.
        rows = await memory_recall_episodes(user_id=user_id, since_ts=since_ts,
                                            limit=limit, order="newest",
                                            exclude_surfaced=True)
        if not rows:
            return None
        lines, total = [], 0.0
        for e in rows:
            total += float(e.spend_usd or 0)
            lines.append(f"- {e.kind}:{e.outcome or '?'} ${float(e.spend_usd or 0):.2f} "
                         f"\"{(e.task or '')[:60]}\"")
        # C6: wrap ONLY the data rows (task text is attacker-authorable). The
        # summary and the instruction stay OUTSIDE the wrapper — an instruction
        # inside a "treat this as data" block cancels itself (T1-01 class).
        data = "\n".join(lines)
        try:
            from core.security.untrusted_wrap import wrap_untrusted
            data = wrap_untrusted("recent_activity", data)
        except Exception:
            pass
        body = (data
                + f"\n({len(rows)} runs in last {window_hours}h, ${total:.2f} total). "
                  "Call recent_activity(since=\"8h\") for more/older.")
        from modules.llm.messages import make_control_message, MessageOrigin
        return make_control_message(
            f"<recent_activity window=\"last {window_hours}h\">\n{body}\n</recent_activity>",
            MessageOrigin.EPISODIC_DIGEST)
    except Exception:
        logger.warning("build_activity_digest failed", exc_info=True)
        return None


def _rail_of(e: Any) -> Optional[str]:
    """``cron:<id>`` / ``goal:<id>`` for an episode, or None (legacy rows name the
    job in ``meta.job_id`` and the goal in ``goal_id``)."""
    tk = str(getattr(e, "thread_key", None) or "")
    if tk.startswith(("cron:", "goal:")):
        return tk
    job = str((getattr(e, "meta", None) or {}).get("job_id") or "")
    if job:
        return f"cron:{job}"
    goal = str(getattr(e, "goal_id", None) or "")
    return f"goal:{goal}" if goal else None


def rail_status(rail: str, data_dir: Optional[str] = None) -> Optional[tuple]:
    """``(status, stopped)`` of a job/goal NOW, read from its live store
    (``cron.db`` / ``goals.db``), or None when there is no store to read.

    A run record is history; whether its job still runs is harness state. A
    missing row reads ``deleted``; an unreadable store reads ``unreadable`` —
    never the same answer as "it runs"."""
    import os
    from core.runtime_paths import cron_db_path, goals_db_path
    kind, _, ident = (rail or "").partition(":")
    if kind not in ("cron", "goal") or not ident:
        return None
    db = cron_db_path(data_dir) if kind == "cron" else goals_db_path(data_dir)
    if not os.path.exists(db):
        return None
    sql = ("SELECT status, enabled FROM cron_jobs WHERE id=?" if kind == "cron"
           else "SELECT status, 1 FROM goals WHERE id=?")
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(db, sql, (ident,), fetch="all") or []
    except Exception:
        return ("unreadable", False)
    if not rows:
        return ("deleted", True)
    status, enabled = str(rows[0][0] or "?"), bool(rows[0][1])
    if kind == "cron":
        stopped = status in ("cancelled", "done") or not enabled
    else:
        from core.goal_vocab import LIVE_STATUSES
        stopped = status not in LIVE_STATUSES
    return (status, stopped)


def _status_note(e: Any, data_dir: Optional[str]) -> str:
    try:
        rail = _rail_of(e)
        st = rail_status(rail, data_dir) if rail else None
    except Exception:
        st = None
    if not st:
        return ""
    what = "job" if rail.startswith("cron:") else "goal"
    return f" ({what} now: {st[0]}{' — STOPPED' if st[1] else ''})"


async def build_mission_continuity(*, user_id: Optional[str], window_hours: int = 24,
                                   limit: int = 8, data_dir: Optional[str] = None):
    """Carry recent autonomous activity INTO the next goal/cron tick (§7.5).

    Mirror-image of ``build_activity_digest``: this one is FOR autonomous sessions
    (the caller confirms ``is_autonomous`` and never sub-agent), so a tick knows what
    it already did/attempted and stops re-deriving 'nothing new' from scratch. Gated
    ``AUTONOMOUS_CONTINUITY_BRIDGE`` (default OFF). Fail-open. Does NOT exclude
    already-surfaced episodes — a tick genuinely needs to see everything it did.
    """
    try:
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.autonomous_continuity_bridge():
            return None
        from modules.memory.registry import memory_recall_episodes
        since_ts = int(time.time()) - window_hours * 3600
        rows = await memory_recall_episodes(user_id=user_id, since_ts=since_ts,
                                            limit=limit, order="newest")
        if not rows:
            return None
        # The job's/goal's CURRENT status rides next to each past run: a run of
        # a since-cancelled job is history, not work to build on.
        lines = [f"- {e.kind}:{e.outcome or '?'}{_status_note(e, data_dir)} "
                 f"\"{(e.task or '')[:70]}\"" for e in rows]
        # C6: rows inside the wrapper, the instruction outside it.
        data = "\n".join(lines)
        try:
            from core.security.untrusted_wrap import wrap_untrusted
            data = wrap_untrusted("recent_activity", data)
        except Exception:
            pass
        body = (data
                + f"\n({len(rows)} recent runs in last {window_hours}h). "
                  "Build on this — do NOT repeat work already done; if it's all covered, "
                  "surface the blocker/next step rather than re-deriving 'nothing new'. "
                  "A run marked STOPPED belongs to a job or goal that is stopped now: do "
                  "not build on it and do not re-create it.")
        from modules.llm.messages import make_control_message, MessageOrigin
        return make_control_message(
            f"<mission_continuity window=\"last {window_hours}h\">\n{body}\n</mission_continuity>",
            MessageOrigin.EPISODIC_DIGEST)
    except Exception:
        logger.warning("build_mission_continuity failed", exc_info=True)
        return None


# ── The run record (2026-10-13 memory/coherency review, R2 + R3) ─────────────
#
# A cron job or a goal is ONE thing that runs many times. Before this, a run
# saw nothing of its own previous runs: a run cut by its time cap or a pause
# wrote no episode (E-B5), a run that BOUGHT and then ran out of steps was
# recorded "failed" with no summary (C-03), and the only path back was a weak
# keyword search (E-B6). The record is an EPISODE (the one episode store, keyed
# ``thread_key = cron:<id> | goal:<id>``) written on EVERY exit path, with what
# the run sent the owner, what it produced and any tx hash; the next run of the
# same job/goal reads the newest ones as ``<previous-runs>``. Whether a session
# kind keeps a record is the regime seam's ``run_record`` column — never a
# second heuristic here.

import json as _json
import re as _re

_TX_RE = _re.compile(r"\b0x[0-9a-fA-F]{64}\b")
_SENT_KEPT, _SENT_CHARS = 3, 200
_OUTPUTS_KEPT, _OUTPUT_CHARS = 5, 100
_TX_KEPT = 5
_META_BUDGET = 3200          # the store drops a meta over 4000 chars whole
PREVIOUS_RUNS_KEPT = 3
PREVIOUS_RUNS_MAX_CHARS = 2000

# C-3 (2026-10-14 review): a run's note for the next run sat at char 1,267 of a
# 1,601-char done text and the block rendered the first 300 chars. The summary
# is clipped head+tail (agents put their notes at the end), and the run's tool
# errors (the ``ERROR:`` ledger lines) render first.
_ERRORS_KEPT, _ERROR_CHARS = 2, 160
_RUN_LEAD_CHARS = 320        # errors of one run, rendered first
_SUMMARY_CHARS, _SUMMARY_CHARS_AFTER_LEAD = 300, 160


def extract_errors(run: Any, limit: int = _ERRORS_KEPT) -> list:
    """The run's tool errors: the evidence tail plus the ``ERROR:`` ledger lines."""
    out: list = []
    try:
        ev = getattr(run, "evidence", None)
        lines = list(getattr(ev, "errors_tail", None) or [])
        lines += [x for x in (getattr(ev, "ledger", None) or []) if "ERROR:" in str(x)]
        for x in lines:
            line = _one_line(str(x).replace(" -> ERROR:", ":"), _ERROR_CHARS)
            if line not in out:
                out.append(line)
    except Exception:
        logger.debug("run record errors: partial read", exc_info=True)
    return out[-limit:]


def run_record_on(regime: str) -> bool:
    """The regime seam's answer: does this kind of session keep a run record?"""
    try:
        from agents.task.session_class import REGIME_POLICY
        return bool(REGIME_POLICY[regime].run_record)
    except Exception:
        return False


def clip_keep_end(text: Optional[str], limit: int = 600) -> str:
    """The ONE head+tail clip (``agents.task.goals.board_types.clip_keep_end``)."""
    from agents.task.goals.board_types import clip_keep_end as _clip
    return _clip(text, limit)


def _one_line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def run_record_facts(run: Any) -> dict:
    """``{sent, outputs, tx}`` from a ``RunOutcome`` (None-safe, never raises).

    ``sent`` = the run's own ``send_message`` texts (newest last); ``outputs`` =
    the real-output actions of the evidence pack (posts, sends, writes, swaps);
    ``tx`` = every 0x-hash the run's results, sends or record name."""
    out = {"sent": [], "outputs": [], "tx": []}
    if run is None:
        return out
    try:
        msgs = [m for m in (getattr(run, "user_messages", None) or []) if str(m or "").strip()]
        out["sent"] = [_one_line(m, _SENT_CHARS) for m in msgs[-_SENT_KEPT:]]
        texts = list(msgs) + [str(getattr(run, "done_text", "") or "")]
        for a in getattr(run, "artifacts", None) or []:
            if isinstance(a, dict) and a.get("kind"):
                detail = str(a.get("detail") or "")
                texts.append(detail)
                if len(out["outputs"]) < _OUTPUTS_KEPT:
                    out["outputs"].append(_one_line(f"{a['kind']}: {detail}", _OUTPUT_CHARS))
        ev = getattr(run, "evidence", None)
        if ev is not None:
            texts.extend(str(x) for x in (getattr(ev, "ledger", None) or []))
            texts.extend(str(x) for x in (getattr(ev, "captured_refs", None) or []))
        seen = []
        for t in texts:
            for h in _TX_RE.findall(t or ""):
                if h.lower() not in (x.lower() for x in seen):
                    seen.append(h)
        out["tx"] = seen[:_TX_KEPT]
    except Exception:
        logger.debug("run record facts: partial read", exc_info=True)
    return out


def did_something(facts: dict) -> bool:
    """A run that sent, produced or signed anything did real work — a stop
    before ``done()`` is then PARTIAL, never a bare failure (C-03)."""
    return bool(facts.get("sent") or facts.get("outputs") or facts.get("tx"))


def _fit_meta(meta: dict) -> dict:
    """Trim the list fields until the JSON fits the store's meta cap."""
    m = dict(meta)
    for key in ("outputs", "sent", "skills_loaded", "tx", "errors"):
        while len(_json.dumps(m)) > _META_BUDGET and m.get(key):
            m[key] = list(m[key])[:-1]
    if len(_json.dumps(m)) > _META_BUDGET:
        m.pop("error", None)
    return m


def _summary_for(outcome: str, reason: str, error: str, facts: dict) -> str:
    bits = [f"{outcome.upper()}: {reason}" if reason else outcome.upper()]
    if error:
        bits.append(f"error: {clip_keep_end(error, 400)}")
    if facts.get("outputs"):
        bits.append("did: " + "; ".join(facts["outputs"]))
    if facts.get("sent"):
        bits.append(f'last sent to the owner: "{facts["sent"][-1]}"')
    if facts.get("tx"):
        bits.append("tx: " + ", ".join(facts["tx"]))
    return " | ".join(bits)[:2000]


async def record_run(*, regime: str, kind: str, session_id: Optional[str],
                     user_id: Optional[str], rail_id: str, task: Optional[str],
                     outcome: str, reason: str = "", error: str = "",
                     run: Any = None, summary: Optional[str] = None,
                     goal_id: Optional[str] = None,
                     meta: Optional[dict] = None,
                     mandate: Any = None) -> None:
    """Write the run record for one exit path. Never raises.

    ``mandate`` (086 W6): the run's trading mandate id (``payload.mandate``),
    stamped on ``meta["mandate"]`` so the mandate's LLM cost reads per run.

    ``summary=None`` composes one from the outcome, the reason, the error and
    the run's facts (a run that stopped before ``done()`` has no done text, and
    an empty summary was the C-03 hole). A done run passes its result text."""
    if not session_id:
        return
    try:
        on = run_record_on(regime)
        facts = run_record_facts(run) if on else {}
        m = dict(meta or {})
        if mandate is not None and str(mandate).strip():
            m["mandate"] = str(mandate).strip()
        if on:
            m.update({k: v for k, v in facts.items() if v})
            if reason:
                m["reason"] = _one_line(reason, 300)
            if error:
                m["error"] = clip_keep_end(error, 400)
            _stop = getattr(run, "stop_reason", None)  # 083 WS-1
            if isinstance(_stop, str) and _stop:
                m["stop_reason"] = _stop
        if summary is None and on:
            summary = _summary_for(outcome, reason, error, facts)
        if on:
            errors = extract_errors(run)
            if errors:
                m["errors"] = errors
        from modules.memory.episodic import finalize_episode
        await finalize_episode(
            session_id=session_id, user_id=user_id, kind=kind, task=task,
            outcome=outcome, summary=summary,
            spend_usd=float(getattr(run, "spend_usd", 0.0) or 0.0),
            steps=int(getattr(run, "steps", 0) or 0),
            artifacts=list(getattr(run, "artifacts", None) or []),
            skills_loaded=list(getattr(run, "skills_loaded", None) or []),
            skills_activation=dict(getattr(run, "skills_activation", None) or {}),
            goal_id=goal_id, thread_key=(rail_id or None) if on else None,
            meta=_fit_meta(m) if on else m, run_record=on)
    except Exception:
        logger.warning("run record write failed (%s)", rail_id, exc_info=True)


def _own_rows(rows: list, rail_id: str) -> list:
    """Legacy rows (written before the record carried ``thread_key``) still
    name their job/goal in ``meta.job_id`` / ``goal_id``."""
    kind, _, ident = rail_id.partition(":")
    out = []
    for e in rows or []:
        meta = getattr(e, "meta", None) or {}
        if kind == "cron" and str(meta.get("job_id") or "") == ident:
            out.append(e)
        elif kind == "goal" and str(getattr(e, "goal_id", "") or "") == ident:
            out.append(e)
    return out


def _hhmm_utc(ts: Any) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%MZ")
    except Exception:
        return "?"


def _render_run(e: Any) -> str:
    meta = getattr(e, "meta", None) or {}
    outcome = str(getattr(e, "outcome", None) or "?").upper()
    reason = meta.get("reason") or ""
    head = f"- {_hhmm_utc(getattr(e, 'ts', None))} {outcome}" + (f" ({_one_line(reason, 120)})" if reason else "")
    summary = " ".join(str(getattr(e, "summary", None) or "").split())
    # C-3: the run's tool errors FIRST (``refusals`` = a legacy row's key), then
    # the summary clipped head+tail.
    lead = [f"error: {x}" for x in (meta.get("errors") or meta.get("refusals") or [])]
    lead_text = _one_line(" | ".join(lead), _RUN_LEAD_CHARS) if lead else ""
    limit = _SUMMARY_CHARS_AFTER_LEAD if lead_text else _SUMMARY_CHARS
    body = clip_keep_end(summary, limit) if summary else "(no summary recorded)"
    lines = [head + ": " + (f"{lead_text}\n  summary: {body}" if lead_text else body)]
    if meta.get("sent"):
        lines.append(f'  last sent to the owner: "{_one_line(meta["sent"][-1], 200)}"')
    if meta.get("outputs"):
        lines.append("  did: " + _one_line("; ".join(meta["outputs"]), 300))
    if meta.get("tx"):
        lines.append("  tx: " + ", ".join(str(h) for h in meta["tx"]))
    return "\n".join(lines)


async def render_previous_runs(*, user_id: Optional[str], rail_id: str, regime: str,
                               limit: int = PREVIOUS_RUNS_KEPT) -> str:
    """The ``<previous-runs>`` block for the NEXT run of ``rail_id``, or ``""``.

    Gated by the regime seam (``run_record``). Fail-open: an unreadable store
    renders one honest line, never an empty block that reads as "first run"."""
    if not rail_id or not run_record_on(regime):
        return ""
    try:
        from modules.memory.registry import memory_recall_episodes
        rows = await memory_recall_episodes(user_id=user_id, thread_key=rail_id,
                                            limit=limit, order="newest", raise_errors=True)
        if len(rows) < limit:
            kind = rail_id.split(":", 1)[0]
            legacy = _own_rows(await memory_recall_episodes(
                user_id=user_id, kind=kind, limit=60, order="newest",
                raise_errors=True), rail_id)
            seen = {getattr(e, "session_id", None) for e in rows}
            rows = list(rows) + [e for e in legacy if getattr(e, "session_id", None) not in seen]
            rows.sort(key=lambda e: float(getattr(e, "ts", 0) or 0), reverse=True)
            rows = rows[:limit]
    except Exception as e:
        logger.warning("previous-runs read failed for %s", rail_id, exc_info=True)
        return (f'<previous-runs rail="{rail_id}">\nCould not read this '
                f"{'job' if rail_id.startswith('cron:') else 'goal'}'s earlier runs "
                f"({type(e).__name__}). Do not assume this is the first run: check what "
                f"is already done (ledger, sent messages) before you act.\n</previous-runs>")
    if not rows:
        return ""
    data, used = [], 0
    for e in rows:
        line = _render_run(e)
        if used + len(line) > PREVIOUS_RUNS_MAX_CHARS and data:
            break
        data.append(line)
        used += len(line) + 1
    body = "\n".join(data)
    try:
        from core.security.untrusted_wrap import wrap_untrusted
        body = wrap_untrusted("previous_runs", body)
    except Exception:
        pass
    what = "job" if rail_id.startswith("cron:") else "goal"
    return (f'<previous-runs rail="{rail_id}">\n'
            f"Your own last {len(data)} run(s) of this {what}, newest first. What a run "
            f"sent, posted or signed HAPPENED — do not do it again; continue from where "
            f"it stopped. A PARTIAL run stopped before done().\n{body}\n</previous-runs>")
