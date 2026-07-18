"""Surface-neutral recap core (owner-UX P4 T1).

Pure data-gathering for "what has the agent done / earned / learned / changed"
over a trailing time window. Extracted from ``cli/ui/commands/h_journey.py``
so the CLI (``/journey`` REPL command + ``polyrob journey``) becomes a thin
rendering consumer over ONE shared core — and any future recap surface
(Telegram/email digest, webview, ...) can reuse the exact same assembly logic
instead of re-implementing it.

Every source (episodes / durable event log / authored skills / unified
ledger) is read through a fail-open seam function, mirroring the discipline
already established in ``h_journey.py``: a missing provider, a disabled flag,
or a backend error never raises — that section is simply empty. The ONE
exception is the time-window string itself: a malformed (non-empty) window
raises a clear ``ValueError`` rather than silently degrading to "all time" —
telling the caller beats guessing.

Paths are explicit, not reached for via a CLI-layer singleton: callers pass
``data_home`` (whatever data root/config they already resolved) rather than
this module reaching into ``core.container``/``pm()``/CLI config itself.

No ``from __future__ import annotations`` (kept consistent with the CLI
command modules this feeds; unnecessary here).
"""
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# A parsed window beyond this is almost certainly a typo (e.g. a stray extra
# digit), not a deliberate "show me a decade" request — reject it the same
# way a malformed label is rejected, rather than silently querying "all of
# recorded history" against a huge (or infinite/NaN) since_ts.
_MAX_WINDOW_SECONDS = 10 * 365 * 86400  # ~10 years


@dataclass
class RecapEntry:
    """One line-item in a recap: a timestamped, human-readable fact."""
    ts: float
    kind: str
    text: str
    amount: Optional[float] = None


def _parse_window(label: str) -> Optional[float]:
    """Parse '30m'/'24h'/'7d'/a bare number of seconds -> seconds.

    A falsy label (``None``/``""``) means "no window" -> ``None`` (all time).
    A non-empty label that doesn't parse raises ``ValueError`` — ported from
    ``h_journey._window_seconds`` but stricter: that REPL-display helper
    fails open (bad input -> "all time"); this core function is the one that
    actually bounds the data query, so a typo must be surfaced, not silently
    widened into a full-history read.

    Also rejects a label that DOES parse but to something unusable as a time
    window: non-finite (``nan``/``inf`` — e.g. ``"1e400d"`` overflows to
    ``inf``, ``"nand"`` parses as ``float("nan")`` because the trailing char
    happens to be the 'd' suffix), absurdly large (> ``_MAX_WINDOW_SECONDS``,
    e.g. a 30-digit day count), or non-positive (``"-24h"`` puts ``since_ts``
    in the FUTURE — a silent empty recap; ``"0h"`` is equally meaningless as
    a duration). Previously this relied on ``int(nan)``/huge
    floats coincidentally raising somewhere downstream; this surface (Telegram
    ``/recap``) exposes the raw label to chat input, so the guard belongs here
    where it produces the same friendly ``ValueError`` as any other malformed
    window.
    """
    if not label:
        return None
    raw = label.strip().lower()
    try:
        if raw.endswith("m"):
            seconds = float(raw[:-1]) * 60
        elif raw.endswith("h"):
            seconds = float(raw[:-1]) * 3600
        elif raw.endswith("d"):
            seconds = float(raw[:-1]) * 86400
        else:
            seconds = float(raw)
    except Exception:
        raise ValueError(
            f"invalid recap window {label!r} (expected e.g. '30m' / '24h' / '7d')")
    if not math.isfinite(seconds) or seconds > _MAX_WINDOW_SECONDS or seconds <= 0:
        raise ValueError(
            f"invalid recap window {label!r}: window must be a positive duration "
            f"(expected e.g. '30m' / '24h' / '7d', max ~10 years)")
    return seconds


def _episodes(user_id: str, since_ts: Optional[float]) -> List[Dict[str, Any]]:
    """Episodes for the tenant (what I did). Seam kept for test monkeypatching;
    the read lives on the shared layer (core/activity_evidence.py, T8)."""
    from core.activity_evidence import recent_episodes
    return recent_episodes(user_id, since_ts)


def _events(user_id: str, since_ts: Optional[float],
            data_home: Optional[str] = None) -> List[Dict[str, Any]]:
    """Durable event-log rows for the tenant (changed/earned). Fail-open.

    Takes an explicit ``data_home`` so a caller with its own resolved data
    root never needs the ``get_event_log()`` singleton/env-var resolution —
    that fallback only kicks in when no home is given (library/legacy use).
    """
    try:
        from agents.task.telemetry.event_log import (
            TelemetryEventLog, get_event_log, event_log_enabled)
        if not event_log_enabled():
            return []
        if data_home:
            log = TelemetryEventLog(os.path.join(data_home, "telemetry_events.db"))
        else:
            log = get_event_log()
        return log.query(user_id=user_id, since_ts=since_ts, limit=200) or []
    except Exception:
        return []


def _authored(user_id: str, data_home: Optional[str] = None) -> List[Dict[str, Any]]:
    """Authored skills + reuse counts for the tenant (learned). Fail-open."""
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        return get_skill_usage_store(data_home).list_authored(user_id=user_id) or []
    except Exception:
        return []


def _ledger(user_id: str, days: int) -> Dict[str, Any]:
    """Unified ledger rollup for the tenant (earned/spent). Fail-open to ``{}``.

    ``{}`` (or an all-zero rollup) means "nothing to show" — build_recap emits
    NO ledger entry in that case, so a broken/absent data layer is never
    rendered as an honest-looking $0.00 (H14b). Partial degradation (some legs
    read, some absent) is annotated onto the entry via
    ``ledger_availability_note``.
    """
    from core.activity_evidence import ledger_rollup
    return ledger_rollup(user_id, days)


def build_recap(user_id: str, data_home: Optional[str] = None,
                window: str = "24h") -> List[RecapEntry]:
    """Assemble one flat, newest-first recap over episodes/events/skills/ledger.

    Pure data assembly: explicit deps/paths, no CLI-layer singletons. Raises
    ``ValueError`` for a malformed (non-empty) ``window``; an empty/``None``
    window means "all time".
    """
    secs = _parse_window(window)
    since_ts = (time.time() - secs) if secs else None
    days = max(1, int((secs or 7 * 86400) // 86400))
    now = time.time()

    entries: List[RecapEntry] = []

    for e in _episodes(user_id, since_ts):
        spend = float(e.get("spend_usd") or 0.0)
        task = (e.get("task") or "").strip().replace("\n", " ")[:80]
        spend_suffix = f" ${spend:.2f}" if spend else ""
        text = f"{e.get('kind') or '?'}:{e.get('outcome') or '?'}{spend_suffix}"
        if task:
            text += f' "{task}"'
        entries.append(RecapEntry(
            ts=float(e.get("ts") or now), kind="episode", text=text,
            amount=spend or None))

    for ev in _events(user_id, since_ts, data_home):
        attrs = ev.get("attrs") or {}
        raw_kind = ev.get("kind") or "event"
        if raw_kind == "self_modification":
            what = attrs.get("kind") or attrs.get("action") or "self_modification"
            ident = attrs.get("skill_id") or attrs.get("id") or ""
            text = f"{what} {ident}".strip()
        else:
            text = raw_kind
        entries.append(RecapEntry(ts=float(ev.get("ts") or now), kind=raw_kind, text=text))

    for s in _authored(user_id, data_home):
        sid = s.get("skill_id") or "?"
        loads = s.get("load_count") if s.get("load_count") is not None else s.get("loads", 0)
        by = s.get("created_by") or ""
        by_suffix = f" [{by}]" if by else ""
        text = f"{sid}{by_suffix} (used {loads}x)"
        entries.append(RecapEntry(ts=float(s.get("created_at") or now), kind="skill", text=text))

    ledger = _ledger(user_id, days)
    if ledger:
        # Two ledgers that must NEVER be summed: treasury is the agent's own
        # USDC (income/spend/net), runtime is the owner's LLM/API bill (no
        # net — there's nothing to net an expense against). Reading the old
        # merged `total_spend_usd`/`net_usd` here reported the owner's API
        # bill as the agent's own P&L (the 2026-07-16 bug this fixes).
        t = ledger.get("treasury") or {}
        r = ledger.get("runtime") or {}
        income = float(t.get("income_usd") or 0.0)
        t_spend = float(t.get("spend_usd") or 0.0)
        net = float(t.get("net_usd") or 0.0)
        pending = float(t.get("pending_usd") or 0.0)
        settled = int(ledger.get("settled_payments") or 0)
        runtime = float(r.get("spend_window_usd") or 0.0)
        calls = int(r.get("calls_window") or 0)
        # An all-zero ledger is NOT activity: on a fresh tenant build_ledger
        # succeeds with zeros and would inject a noise "$0.00" entry, defeating
        # format_recap_markdown's "nothing to report" branch (and making the
        # empty-recap path depend on whether the global DB happens to be
        # initialized). h_journey renders its own $0 fallback line, so the
        # /journey surface is unaffected by skipping the entry here. Runtime
        # spend/calls are included — runtime activity with zero treasury
        # activity (e.g. a pure-cost day) is still activity.
        if any((income, t_spend, net, settled, pending, runtime, calls)):
            text = (f"Income: ${income:.2f} ({settled} settled) · "
                    f"spend ${t_spend:.2f} · net ${net:.2f} · "
                    f"runtime ${runtime:.2f}")
            # H14b: if a ledger leg could not be read (or wallet metering is
            # off), say so ON the entry — a partially-degraded rollup must not
            # present its zeroed legs as real $0.00 next to real activity.
            try:
                from modules.credits.unified_ledger import ledger_availability_note
                note = ledger_availability_note(ledger)
                if note:
                    text += f" ⚠ {note}"
            except Exception:
                pass
            entries.append(RecapEntry(ts=now, kind="ledger", text=text, amount=net))

    entries.sort(key=lambda r: r.ts, reverse=True)
    return entries


def format_recap_markdown(entries: List[RecapEntry], window: str) -> str:
    """Plain markdown rendering of a recap (no Rich) — a friendly empty-state
    line when there's nothing to show. Generic one-line-per-entry shape; a
    surface wanting a richer/grouped layout (e.g. ``/journey``) builds its own
    view on top of the same ``entries`` list instead of this formatter."""
    scope = window.strip() if window and window.strip() else "all time"
    lines = [f"# Recap — {scope}", ""]
    if not entries:
        lines.append("_Nothing to report in this window._")
        return "\n".join(lines)
    for e in entries:
        amount_suffix = f" (${e.amount:.2f})" if e.amount else ""
        lines.append(f"- **{e.kind}**: {e.text}{amount_suffix}")
    return "\n".join(lines)
