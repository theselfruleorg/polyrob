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
PREVIOUS_RUNS_KEPT = 5       # R16: a compact track record, this job only
PREVIOUS_RUNS_MAX_CHARS = 2000   # 108 W3: the whole block, frame and job sheet included
_RUN_HEAD_CHARS = 110        # one run when the block has no room for its detail

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


#: 095 W11 / R1: where a run's outcome signal came from, most trusted first. A
#: receipt (the run sent, produced or signed something) beats the owner's word,
#: which beats the completion judge, which beats the agent's own report. Only
#: the first three may teach; ``self`` is recorded, never learned from.
SIGNAL_SOURCES = ("receipt", "owner", "judge", "self", "none")


def verified_state(value: Any) -> Optional[bool]:
    """``meta["verified"]`` as one answer: a cron run's bool or a goal's word. ``None`` when
    nothing checked the run."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip()
    return (text == "verified") if text else None


def signal_source(meta: dict, *, useful: bool, run: Any = None,
                  summary: Optional[str] = None) -> str:
    """The most trusted signal this run record carries (:data:`SIGNAL_SOURCES`).

    ``owner`` = ``meta["owner_verdict"]`` holds a confirm or a correct (a neutral
    reply says nothing about the run); ``judge`` = ``meta["verified"]``, a goal's
    judge or a cron job's ``expect`` check; ``self`` = only the agent's own done
    text or summary. Never raises."""
    try:
        if useful:
            return "receipt"
        if str((meta or {}).get("owner_verdict") or "").strip() not in ("", "neutral"):
            return "owner"
        if verified_state((meta or {}).get("verified")) is not None:
            return "judge"
        if str(getattr(run, "done_text", "") or "").strip() or str(summary or "").strip():
            return "self"
    except Exception:
        logger.debug("signal source: partial read", exc_info=True)
    return "none"


def rail_payload(rail: str, data_dir: Optional[str] = None) -> dict:
    """The payload of a job or goal, from its live store; ``{}`` when it is gone."""
    import os
    from core.runtime_paths import cron_db_path, goals_db_path
    kind, _, ident = (rail or "").partition(":")
    if kind not in ("cron", "goal") or not ident:
        return {}
    db = cron_db_path(data_dir) if kind == "cron" else goals_db_path(data_dir)
    if not os.path.exists(db):
        return {}
    table = "cron_jobs" if kind == "cron" else "goals"
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(db, f"SELECT payload FROM {table} WHERE id=?", (ident,),
                             fetch="all") or []
        payload = _json.loads(rows[0][0] or "{}") if rows else {}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        logger.debug("rail payload unreadable for %s", rail, exc_info=True)
        return {}


def rail_lane(rail: str, data_dir: Optional[str] = None) -> str:
    """The job's or goal's lane (``core.cron_lanes.job_lane`` of its payload);
    ``""`` when there is no lane or no store."""
    from core.cron_lanes import job_lane
    payload = rail_payload(rail, data_dir)
    return job_lane(payload) if payload else ""


def _skill_generations(skills: list, user_id: Optional[str]) -> dict:
    """``{skill_id: patch_generation}`` of the skills a run loaded — the join of
    a real outcome to the revision that produced it. ``{}`` when unreadable."""
    ids = sorted({str(s) for s in skills or [] if str(s or "").strip()})
    if not ids or not user_id:
        return {}
    try:
        from modules.skills.skill_usage import existing_skill_usage_store
        store = existing_skill_usage_store()
        return store.patch_generations(ids, str(user_id)) if store else {}
    except Exception:
        logger.debug("skill generations unreadable", exc_info=True)
        return {}


def _fit_meta(meta: dict) -> dict:
    """Trim the list fields until the JSON fits the store's meta cap."""
    m = dict(meta)
    if len(_json.dumps(m)) > _META_BUDGET:
        m.pop("skill_gens", None)
    if len(_json.dumps(m)) > _META_BUDGET:
        m.pop("compaction_summary", None)   # lossy: what the run did outranks it
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
                     mandate: Any = None) -> Optional[dict]:
    """Write the run record for one exit path. Never raises. Returns the meta it
    stored (095 W13: the after-run reviewer reads its signal), None on failure.

    ``mandate`` (086 W6): the run's trading mandate id (``payload.mandate``),
    stamped on ``meta["mandate"]`` so the mandate's LLM cost reads per run.

    ``summary=None`` composes one from the outcome, the reason, the error and
    the run's facts (a run that stopped before ``done()`` has no done text, and
    an empty summary was the C-03 hole). A done run passes its result text."""
    if not session_id:
        return None
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
            # 095 W11: the outcome signal on the record — ``useful`` is the ONE
            # predicate (``did_something``, the cron_run event's too), and
            # ``signal_source`` says how far the outcome can be trusted.
            useful = did_something(facts)
            m["useful"] = useful
            m["signal_source"] = signal_source(m, useful=useful, run=run, summary=summary)
            if not m.get("lane"):
                lane = rail_lane(rail_id)
                if lane:
                    m["lane"] = lane
            compacted = getattr(run, "compaction_summary", "")
            if isinstance(compacted, str) and compacted:
                m["compaction_summary"] = compacted   # 108 W7: bounded by the compactor
            gens = _skill_generations(getattr(run, "skills_loaded", None) or [], user_id)
            if gens:
                m["skill_gens"] = gens
        if summary is None and on:
            summary = _summary_for(outcome, reason, error, facts)
        if on:
            errors = extract_errors(run)
            if errors:
                m["errors"] = errors
        from modules.memory.episodic import finalize_episode
        stored = _fit_meta(m) if on else m
        await finalize_episode(
            session_id=session_id, user_id=user_id, kind=kind, task=task,
            outcome=outcome, summary=summary,
            spend_usd=float(getattr(run, "spend_usd", 0.0) or 0.0),
            steps=int(getattr(run, "steps", 0) or 0),
            artifacts=list(getattr(run, "artifacts", None) or []),
            skills_loaded=list(getattr(run, "skills_loaded", None) or []),
            skills_activation=dict(getattr(run, "skills_activation", None) or {}),
            goal_id=goal_id, thread_key=(rail_id or None) if on else None,
            meta=stored, run_record=on)
        return stored
    except Exception:
        logger.warning("run record write failed (%s)", rail_id, exc_info=True)
        return None


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


def _flag(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v) if v not in (None, "") else "-"


def _track_record(e: Any, meta: dict) -> str:
    """095 W11 (R16): ``verified=<v> useful=<u> stop=<reason> skills=<ids> $<spend>``.
    A skill patched since first use reads ``id@g<generation>``."""
    gens = meta.get("skill_gens") or {}
    ids = [str(s) for s in (meta.get("skills_loaded") or [])][:4]
    skills = ",".join(f"{s}@g{gens[s]}" if gens.get(s) else s for s in ids) or "-"
    try:
        spend = f"${float(getattr(e, 'spend_usd', 0) or 0):.2f}"
    except (TypeError, ValueError):
        spend = "$?"
    verdict = str(meta.get("owner_verdict") or "").strip()
    return (f"verified={_flag(meta.get('verified'))} useful={_flag(meta.get('useful'))} "
            + (f"owner_verdict={verdict} " if verdict else "")
            + f"stop={_flag(meta.get('stop_reason'))} skills={skills} {spend}")


def _run_head(e: Any) -> str:
    meta = getattr(e, "meta", None) or {}
    outcome = str(getattr(e, "outcome", None) or "?").upper()
    reason = meta.get("reason") or ""
    return (f"- {_hhmm_utc(getattr(e, 'ts', None))} {outcome} {_track_record(e, meta)}"
            + (f" ({_one_line(reason, 120)})" if reason else ""))


def _render_run(e: Any) -> str:
    meta = getattr(e, "meta", None) or {}
    head = _run_head(e)
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


def _fit_runs(rows: list, budget: int) -> list:
    """Every run gets its outcome line, the oldest leaving first if even those do not fit.
    The detail of the runs goes to the newest while the budget lasts."""
    short = [_one_line(_run_head(e), _RUN_HEAD_CHARS) for e in rows]
    while len(short) > 1 and sum(len(h) + 1 for h in short) > budget:
        short.pop()
    spare = budget - sum(len(h) + 1 for h in short)
    out = []
    for e, head in zip(rows, short):
        full = _render_run(e)
        extra = len(full) - len(head)
        if extra <= spare:
            out.append(full)
            spare -= max(extra, 0)
        elif spare > 1:
            out.append(full[:len(head) + spare - 1] + "…")
            spare = 0
        else:
            out.append(head)
    return out


def _frame(rail_id: str, lead: str, body: str) -> str:
    try:
        from core.security.untrusted_wrap import wrap_untrusted
        body = wrap_untrusted("previous_runs", body)
    except Exception:
        pass
    return f'<previous-runs rail="{rail_id}">\n{lead}\n{body}\n</previous-runs>'


async def _read_own_runs(user_id: Optional[str], rail_id: str, limit: int) -> list:
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
    return rows


async def render_previous_runs(*, user_id: Optional[str], rail_id: str, regime: str,
                               limit: int = PREVIOUS_RUNS_KEPT,
                               data_dir: Optional[str] = None) -> str:
    """The ``<previous-runs>`` block for the NEXT run of ``rail_id``, or ``""``. For a cron
    job it is the job sheet: its outcomes plus what ``job_sheet.job_sections`` reads.

    Gated by the regime seam (``run_record``). At most :data:`PREVIOUS_RUNS_MAX_CHARS`,
    frame included. It carries stored times only, never the clock of this run. Fail-open:
    an unreadable store renders one honest line, never an empty block that reads as
    "first run"."""
    if not rail_id or not run_record_on(regime):
        return ""
    what = "job" if rail_id.startswith("cron:") else "goal"
    rows, read_error = [], None
    try:
        rows = await _read_own_runs(user_id, rail_id, limit)
    except Exception as e:
        logger.warning("previous-runs read failed for %s", rail_id, exc_info=True)
        read_error = type(e).__name__
    sections, has_facts = [], False
    if what == "job":
        from agents.task.agent.core.job_sheet import job_sections
        sections, has_facts = job_sections(job_id=rail_id.split(":", 1)[1], user_id=user_id,
                                           data_dir=data_dir)
    if not rows and not read_error and not has_facts:
        return ""
    if read_error:
        lead = (f"Could not read this {what}'s earlier runs ({read_error}). Do not assume this "
                f"is the first run: check what is already done (ledger, sent messages) before "
                f"you act.")
    else:
        lead = (f"Your own last {len(rows)} run(s) of this {what}, newest first. What a run "
                f"sent, posted or signed HAPPENED — do not do it again; continue from where "
                f"it stopped. A PARTIAL run stopped before done().")
    if sections:
        lead += " The stores wrote this sheet: read no file for these facts."
    if read_error and not sections:
        return f'<previous-runs rail="{rail_id}">\n{lead}\n</previous-runs>'
    tail = "\n".join(sections)
    room = PREVIOUS_RUNS_MAX_CHARS - len(_frame(rail_id, lead, tail)) - 1
    runs = "\n".join(_fit_runs(rows, room)) if rows else ""
    if not rows and not read_error:
        runs = "no earlier run recorded"
    body = "\n".join(x for x in (runs, tail) if x)
    out = _frame(rail_id, lead, body)
    # The frame rewrites a delimiter that the data names, so a row can grow it.
    while len(out) > PREVIOUS_RUNS_MAX_CHARS and body:
        body = body[:max(0, len(body) - (len(out) - PREVIOUS_RUNS_MAX_CHARS) - 1)]
        out = _frame(rail_id, lead, body)
    return out


# ── Per-lane outcome rates (095 W11 step 4) ──────────────────────────────────

LANE_RATE_DAYS = 30
_LANE_RATE_ROWS = 500


async def lane_outcome_rates(*, user_id: Optional[str], days: int = LANE_RATE_DAYS,
                             data_dir: Optional[str] = None) -> list:
    """Per lane, the run records of the last *days*: ``{lane, runs, useful,
    useful_n, verified, verified_n}`` (``*_n`` = the runs that carry that field;
    a row written before the field existed is not counted as a no). Newest lane
    data only, read-only. Raises when the episode store cannot be read — an
    unreadable store is not an empty one."""
    from modules.memory.registry import memory_recall_episodes
    since_ts = int(time.time()) - int(days) * 86400
    rows: list = []
    for kind in ("cron", "goal"):
        rows += await memory_recall_episodes(user_id=user_id, since_ts=since_ts,
                                             kind=kind, limit=_LANE_RATE_ROWS,
                                             order="newest", raise_errors=True)
    lanes: dict = {}
    rail_lanes: dict = {}
    for e in rows:
        meta = getattr(e, "meta", None) or {}
        rail = _rail_of(e)
        if not rail:
            continue
        lane = str(meta.get("lane") or "")
        if not lane:
            if rail not in rail_lanes:
                rail_lanes[rail] = rail_lane(rail, data_dir)
            lane = rail_lanes[rail] or rail
        d = lanes.setdefault(lane, {"lane": lane, "runs": 0, "useful": 0, "useful_n": 0,
                                    "verified": 0, "verified_n": 0})
        d["runs"] += 1
        if isinstance(meta.get("useful"), bool):
            d["useful_n"] += 1
            d["useful"] += int(meta["useful"])
        verified = verified_state(meta.get("verified"))
        if verified is not None:
            d["verified_n"] += 1
            d["verified"] += int(verified)
    return sorted(lanes.values(), key=lambda d: (-d["runs"], d["lane"]))


def render_lane_rates(rates: list, *, days: int = LANE_RATE_DAYS, limit: int = 10) -> str:
    """One line per lane: ``<lane>: <n> runs · useful u/n · verified v/m``."""
    if not rates:
        return f"no job or goal runs recorded in the last {days} days"
    out = []
    for d in rates[:limit]:
        bits = [f"{d['runs']} runs"]
        bits.append(f"useful {d['useful']}/{d['useful_n']}" if d["useful_n"] else "useful -")
        if d["verified_n"]:
            bits.append(f"verified {d['verified']}/{d['verified_n']}")
        out.append(f"  - {d['lane']}: " + " · ".join(bits))
    if len(rates) > limit:
        out.append(f"  - (+{len(rates) - limit} more lanes)")
    return "\n".join(out)
