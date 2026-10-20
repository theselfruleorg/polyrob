"""Status section: ``delivery`` — owner notices, the missed-message bodies,
poll health, the digest, and the outbound queue.

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.status_types import (  # noqa: F401
    HealthItem, MISSED_IN_SNAPSHOT, MISSED_TEXT_CHARS, SEVERITY_CRIT, SEVERITY_WARN,
    Section, _K_OWNER_NOTICE, _K_RUN_DEGRADED, _K_SURFACE_POLL_ERROR, _K_TOOL_DENIED,
    _K_TOOL_TIMEOUT, _K_USER_DELIVERY, _SUPPRESSED_PREFIX, _hhmm, _payload, _tele_rows,
    snapshot_rows as _rows,
)

logger = logging.getLogger("core.status_snapshot")


#: Poll faults in 24h below this are ordinary long-poll turbulence, not news.
_POLL_ERROR_WARN_AT = 6


def _surface_poll_health(tele: Section) -> List[HealthItem]:
    """Inbound TRANSPORT faults on the polling surfaces, in the window.

    2026-09-15 prod review, C11: 76 `telegram get_updates failed` in 7 days —
    34 request timeouts, 28 connection resets, 14 Bad Gateway. The poller
    recovers each time, and that is precisely why no seat ever said so: "the
    agent went quiet for a while" had no reading anywhere. Recovery is not the
    same as health.

    A WARN with a count, not an outage alarm — below ``_POLL_ERROR_WARN_AT``
    this is ordinary turbulence and says nothing.
    """
    if not tele.available:
        return []
    rows = _tele_rows(tele, _K_SURFACE_POLL_ERROR)
    if len(rows) < _POLL_ERROR_WARN_AT:
        return []
    by_surface: Dict[str, int] = {}
    for r in rows:
        sid = r.get("source") or "?"
        by_surface[sid] = by_surface.get(sid, 0) + 1
    where = ", ".join(f"{k}={v}" for k, v in
                      sorted(by_surface.items(), key=lambda kv: -kv[1]))
    return [HealthItem(
        key="surface_poll_errors", severity=SEVERITY_WARN,
        text=f"{len(rows)} inbound poll failure(s) in 24h ({where}) — the agent "
             f"was intermittently unreachable",
        remedy="`polyrob doctor` and the service log; usually upstream turbulence")]


def _digest_health(user_id: str, data_dir: str) -> List[HealthItem]:
    """Is the owner digest ENABLED but has no producer scheduled?

    2026-09-15 prod review, C4. The delivery rail records every suppressed
    owner message as an ``owner_notice`` on the promise that it is "rolled into
    the digest". On prod ``OWNER_DIGEST_ENABLED=true`` and the ONE cron job
    carrying ``payload.digest`` was ``cancelled`` — so 889 suppressed messages
    had no roll-up channel and `/missed` (last 5) was the whole recovery
    surface. An enabled flag with no producer reports healthy and runs nothing.

    An unreadable cron store is reported as UNKNOWN, never as scheduled: "I
    could not look" is a different fact from "it is fine".
    """
    from core.runtime_paths import cron_db_path
    from core.config_policy import AutonomyConfig
    try:
        if not AutonomyConfig.owner_digest_enabled():
            return []
    except Exception as e:
        return [HealthItem(
            key="digest_unknown", severity=SEVERITY_WARN,
            text=f"cannot tell whether the owner digest is on ({type(e).__name__})",
            remedy="`polyrob doctor --flags` for OWNER_DIGEST_ENABLED")]
    try:
        jobs = _rows(cron_db_path(data_dir),
                     "SELECT status, enabled, payload FROM cron_jobs WHERE user_id=?",
                     (user_id,))
    except Exception as e:
        return [HealthItem(
            key="digest_unknown", severity=SEVERITY_WARN,
            text=f"owner digest is enabled but the cron store is unreadable "
                 f"({type(e).__name__}) — cannot tell whether it is scheduled",
            remedy="`polyrob doctor` for the store path, then `polyrob job list`")]
    for row in jobs:
        if not _payload(row).get("digest"):
            continue
        if str(row.get("status") or "") in ("cancelled", "done") or not row.get("enabled"):
            continue
        return []
    return [HealthItem(
        key="digest_unscheduled", severity=SEVERITY_WARN,
        text="owner digest is enabled but no digest job is scheduled — suppressed "
             "messages have no roll-up channel",
        remedy="`polyrob job digest 'every day 08:00'`")]


#: `recent_notices` window: sends by OTHER sessions this recent are "you were
#: just told …" context for a chat turn (2026-09-21: the owner's correction
#: landed 28 s after an autonomous run's notice and was matched to the wrong
#: act). Ten minutes, not three: on 2026-09-22 06:07Z "Explin better" landed
#: 210 s after the EXIT rail's R6 escalation — an owner reads the notice, then
#: types — and the 180 s window missed it, so the chat asked "explain what?".
RECENT_NOTICE_WINDOW_SEC = 600


def _recent_notices(rows: List[Dict[str, Any]], *, session_id: Optional[str],
                    now: float) -> List[Dict[str, Any]]:
    """The last few minutes of DELIVERED owner messages from sessions other than
    *session_id* (newest first, ≤3). Empty without a session — the owner's
    /status has no "you" to tell."""
    if not session_id:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:  # newest-first
        a = r.get("attrs") or {}
        if a.get("outcome") != "sent":
            continue
        if str(r.get("session_id") or "") == str(session_id):
            continue
        ts = float(r.get("ts") or 0)
        if now - ts > RECENT_NOTICE_WINDOW_SEC:
            continue
        out.append({"ts": ts, "session_id": str(r.get("session_id") or ""),
                    "source": str(r.get("source") or "?"),
                    "text": str(a.get("text") or "").strip()[:240]})
        if len(out) >= 3:
            break
    return out


def _missed_bodies(user_id: str, data_dir: str, suppressed: int):
    """``(entries, left_out, unavailable_reason)`` for the undelivered owner
    messages — the BODIES, not just the count.

    Reads the ONE query every `/missed` seat uses
    (``core.surfaces.missed.missed_notices``) rather than carrying a second
    marker list; a marker the rail adds shows up here for free.

    ⚠️ That reader RAISES on an unreadable telemetry store precisely so a
    caller cannot conflate it with "nothing was missed". Here that becomes a
    NAMED reason next to an empty list — a digest told "you missed nothing" by
    a failed read is the confident-wrong class this file exists to prevent.
    The independent `suppressed` count is computed from rows already in hand
    and is unaffected either way.
    """
    try:
        from core.surfaces import missed as missed_mod
        rows = missed_mod.missed_notices(user_id, data_dir, n=MISSED_IN_SNAPSHOT)
    except Exception as e:
        return [], 0, f"{type(e).__name__}: {e}"[:200]
    entries = []
    for r in rows[:MISSED_IN_SNAPSHOT]:
        text = str(r.get("text") or "")
        if len(text) > MISSED_TEXT_CHARS:
            text = text[:MISSED_TEXT_CHARS] + "…"
        entries.append({"ts": float(r.get("ts") or 0.0),
                        "kind": str(r.get("kind") or "capped"),
                        "text": text})
    # The reader is bounded by n, so the remainder is derived from the 24h
    # count rather than from the (already truncated) row list.
    left_out = max(0, int(suppressed) - len(entries))
    return entries, left_out, None


def _delivery_section(user_id: str, data_dir: str, tele: Section, container: Any,
                      now: float, session_id: Optional[str] = None) -> Section:
    sec = Section(name="delivery")
    from core.runtime_paths import prefs_home_dir
    from core.surfaces.user_delivery import _reserved_slots, effective_daily_cap
    # D33: the cap the GATE reads, which resolves `prefs_home_dir()` — the
    # identity axis. Passing `data_dir` read `<data_home>/data` on a server, a
    # shadow no preference writer ever writes to, so `/status` reported the env
    # default while the rail enforced the owner's raised value (or the reverse).
    cap = int(effective_daily_cap(user_id, prefs_home_dir()))
    sec.data["cap"] = cap
    if not tele.available:
        # The cap gate's memory IS the telemetry log: without it we cannot say
        # how many messages were suppressed — say exactly that.
        raise RuntimeError(f"delivery rail memory unreadable ({tele.reason})")
    outcomes: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    rows = _tele_rows(tele, _K_USER_DELIVERY)
    for r in rows:
        o = (r["attrs"] or {}).get("outcome") or "?"
        outcomes[o] = outcomes.get(o, 0) + 1
        if o == "capped":
            src = r.get("source") or "?"
            by_source[src] = by_source.get(src, 0) + 1
    # D46: "cap used" must be the number the GATE counts, so the ONE predicate
    # decides it here too. Every critical/exempt-lane row was included, so the
    # figure routinely read over the cap while nothing was being denied — and
    # an owner reading "41/30 cap used" cannot act on it.
    from core.surfaces.user_delivery import _budgeted, _CONSUMED_OUTCOMES
    _spent = [r for r in rows
              if (r["attrs"] or {}).get("outcome") in _CONSUMED_OUTCOMES]
    consumed = len(_budgeted(_spent))
    uncounted = len(_spent) - consumed
    capped = outcomes.get("capped", 0)
    suppressed_notices = sum(
        1 for r in _tele_rows(tele, _K_OWNER_NOTICE)
        if str((r["attrs"] or {}).get("text") or "").startswith(_SUPPRESSED_PREFIX))
    missed, missed_more, missed_unavailable = _missed_bodies(user_id, data_dir,
                                                             suppressed_notices)
    sec.data.update({"outcomes": outcomes, "consumed": consumed, "capped": capped,
                     "uncounted": uncounted,
                     "capped_by_source": by_source, "suppressed_notices": suppressed_notices,
                     "missed": missed, "missed_more": missed_more,
                     "missed_unavailable": missed_unavailable,
                     "reserved_slots": _reserved_slots(),
                     "recent_notices": _recent_notices(rows, session_id=session_id, now=now)})
    sec.lines.append(
        f"owner messages 24h: {consumed}/{cap} cap used, {capped} suppressed, "
        f"{outcomes.get('fallback', 0)} fallback, {outcomes.get('no_sink', 0)} no sink, "
        f"{outcomes.get('paused', 0)} paused, "
        f"{outcomes.get('deduped', 0)} deduped, {outcomes.get('rate_limited', 0)} rate-limited, "
        f"{outcomes.get('quiet_held', 0)} held (quiet hours)")
    # 061: "am I recording the conversation" — checkable on every seat. A
    # missing store is an honest line, never a zero; it is never created here.
    try:
        from core.surfaces.owner_thread import thread_counts
        _tc = thread_counts(container, user_id, hours=24.0, data_dir=data_dir)
    except Exception:
        _tc = None
    sec.data["owner_thread_24h"] = _tc
    sec.lines.append(
        f"owner thread 24h: {_tc['out']} lines to you, {_tc['in']} from you" if _tc
        else "owner thread: nothing recorded yet")
    if uncounted:
        # D46: its OWN line, never folded into "cap used". These landed and are
        # exempt from the cap by design; hiding them would make the delivered
        # total look smaller than it is.
        sec.lines.append(
            f"plus {uncounted} delivered on an uncapped lane (critical / the "
            f"message tool) — these do not spend the cap")
    # The BODIES, on their own lines. ⚠️ Rendered, not merely stored: the LLM
    # digest job reads `agent_status`, which renders LINES and never touches
    # `section.data`, so a data-only field would have changed nothing for the
    # owner — the "built but not wired" trap this item was filed about.
    if missed_unavailable:
        sec.lines.append(
            f"missed owner messages: could not read them ({missed_unavailable}) "
            f"— do not report this as 'nothing missed'")
    elif missed:
        sec.lines.append(f"missed owner messages ({len(missed) + missed_more}), newest first:")
        for entry in missed:
            body = entry["text"].replace("\n", " ")
            if len(body) > 160:
                body = body[:160] + "…"
            sec.lines.append(
                f"  {_hhmm(entry['ts'])} [{entry['kind']}] {body}")
        if missed_more:
            sec.lines.append(f"  … and {missed_more} more — `/missed` for the full list")
    if capped:
        top = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])[:3])
        sec.health.append(HealthItem(
            key="delivery_capped", severity=SEVERITY_WARN,
            text=f"{capped} owner message(s) suppressed by the daily cap in 24h "
                 f"({consumed}/{cap} used; by source: {top})",
            remedy="/missed to read them; raise the cap with `/config set delivery.daily_cap N`"))
    no_sink = outcomes.get("no_sink", 0)
    if no_sink:
        # 080 WS-4: `no_sink` = nobody received it (no surface could carry it).
        # That is a message the owner never got, not a delivery statistic.
        sec.health.append(HealthItem(
            key="delivery_no_sink", severity=SEVERITY_WARN,
            text=f"{no_sink} owner message(s) in 24h reached nobody — no chat surface "
                 f"could carry them",
            remedy="connect a chat surface (`polyrob surface list`), then /missed"))
    sec.health.extend(_digest_health(user_id, data_dir))
    sec.health.extend(_surface_poll_health(tele))
    timeouts = len(_tele_rows(tele, _K_TOOL_TIMEOUT))
    degraded = len(_tele_rows(tele, _K_RUN_DEGRADED))
    denied = len(_tele_rows(tele, _K_TOOL_DENIED))
    sec.data.update({"tool_timeouts": timeouts, "run_degraded": degraded, "tool_denied": denied})
    sec.lines.append(f"24h governance: {timeouts} tool timeout(s), {denied} tool denial(s), "
                     f"{degraded} degraded run outcome(s)")
    if timeouts:
        sec.health.append(HealthItem(
            key="tool_timeouts", severity=SEVERITY_WARN,
            text=f"{timeouts} tool timeout(s) in 24h", remedy="/recap to see which runs"))
    if degraded:
        sec.health.append(HealthItem(
            key="run_outcome_degraded", severity=SEVERITY_WARN,
            text=f"{degraded} run(s) finished DEGRADED in 24h", remedy="/recap"))
    _outbound_queue_lines(sec, data_dir, container=container)
    # Surface circuits (live objects; only when a container is at hand).
    if container is not None:
        from core.surfaces.health import surface_health
        rows = surface_health(container)
        sec.data["surfaces"] = rows
        for r in rows:
            if r.get("circuit") == "OPEN":
                sec.health.append(HealthItem(
                    key=f"circuit:{r['surface_id']}", severity=SEVERITY_CRIT,
                    text=f"surface {r['surface_id']} circuit is OPEN — sends are being dropped",
                    remedy=f"`polyrob surface resume {r['surface_id']}` once the platform is back"))
            if r.get("dead_targets"):
                sec.lines.append(f"{r['surface_id']}: {r['dead_targets']} dead target(s)")
    return sec


def _outbound_queue_db(data_dir: str) -> str:
    """Where ``outbox.db`` lives — beside the other surface stores.

    ⚠️ ``data_dir``, NOT the telemetry db's directory. ``core/surfaces/
    bootstrap.py`` builds the queue at ``<container.config.data_dir>/
    outbox.db``, beside ``surfaces.db`` / ``correspondents.db`` (which this
    module already resolves the same way). ``telemetry_events.db`` resolves on
    the SIDECAR axis (``core.runtime_paths.sidecar_db_path``, plus a
    ``TELEMETRY_EVENT_LOG_PATH`` override), which on a server is one directory
    ABOVE ``config.data_dir`` — so deriving the queue from it looked in a
    directory the queue is not in and rendered a live queue, dead letters and
    all, as "no outbox.db on this deployment".
    """
    import os as _os
    return _os.path.join(data_dir or ".", "outbox.db")


def _outbound_queue_lines(sec: Section, data_dir: str, container: Any = None) -> None:
    """D22: the durable outbound queue's depth and its DEAD LETTERS.

    ``OutboundDeliveryQueue.counts()`` had no caller anywhere in the tree, so a
    dead-lettered row — a message the queue tried N times and GAVE UP on — was
    a message the owner never received and no seat could name.

    ⚠️ The REGISTERED queue wins when there is one. In the process that owns
    the surface bus the queue is a live service, so asking the container is an
    answer rather than a guess — and it cannot disagree with the object the
    router is actually enqueuing into. The path is the fallback for the seats
    with no bus (``polyrob doctor``, the CLI), and it is derived the way
    ``core/surfaces/bootstrap.py`` derives it.

    ⚠️ A missing ``outbox.db`` renders ``unavailable``, and the read never
    creates it: on a deploy with no durable queue "nothing queued" and "there is
    no queue" are different facts.
    """
    import os as _os
    q = None
    try:
        q = container.get_service("outbound_queue") if container is not None else None
    except Exception as e:
        logger.warning("status: outbound queue service probe failed: %s", e)
        q = None
    path = _outbound_queue_db(data_dir)
    if q is None and not _os.path.exists(path):
        sec.data["outbound_queue"] = None
        # ⚠️ Name WHERE it looked. "no durable queue on this deployment" is a
        # claim about the deployment; all this read knows is that one path held
        # no file and no bus was registered on this seat. A seat whose home
        # resolution differs from the daemon's would otherwise report a live
        # queue, dead letters and all, as a queue that does not exist.
        sec.lines.append(f"outbound queue: unavailable (no bus on this seat and "
                         f"no queue at {path})")
        return
    try:
        if q is None:
            from core.surfaces.outbound_queue import OutboundDeliveryQueue
            q = OutboundDeliveryQueue(path)
        counts = q.counts()
        dead = q.dead_letters(limit=3)
    except Exception as e:
        sec.data["outbound_queue"] = None
        sec.lines.append(f"outbound queue: unavailable ({type(e).__name__}: "
                         f"{str(e)[:120]})")
        return
    sec.data["outbound_queue"] = counts
    sec.data["outbound_dead_letters"] = dead
    sec.lines.append(
        f"outbound queue: {counts.get('pending', 0)} pending, "
        f"{counts.get('inflight', 0)} in flight, "
        f"{counts.get('delivered', 0)} delivered, {counts.get('dead', 0)} dead")
    n_dead = int(counts.get("dead", 0) or 0)
    if n_dead:
        for row in dead:
            sec.lines.append(
                f"  dead: {row.get('surface_id')} -> {row.get('dest')} after "
                f"{row.get('attempts')} attempt(s) — "
                f"{str(row.get('last_error') or 'no error recorded')[:100]}")
        sec.health.append(HealthItem(
            key="outbound_dead_letters", severity=SEVERITY_WARN,
            text=f"{n_dead} outbound message(s) were dead-lettered — the queue "
                 f"gave up and nobody received them",
            remedy="/missed to read what was undelivered; check the surface is "
                   "online, then re-send"))
