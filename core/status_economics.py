"""``economics`` status section — what the model calls cost and how long they take (057 WS-I).

Read-only over ``usage_records`` in ``bot.db`` (the ONE per-call ledger the LLM
clients already write: ``input_tokens``, ``cached_tokens``, ``output_tokens``,
``api_cost_usd``, ``metadata.duration_seconds``). The 2026-09-19 ops report
priced the tool schemas as the cost driver because no seat showed
``cached_tokens``; this section exists so that number is never guessed again.

Invariants (the status SSOT rules): tenant-scoped; an unreadable or absent
ledger renders its reason, never a zero; nothing here reads the network. The
``budget_runway`` health item is attached by the snapshot builder only when a
provider balance was actually read (``include_balances``) — a runway computed
from an unknown balance would be a confident number about nothing.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

WINDOW_SEC_DEFAULT = 86400
RUNWAY_CRIT_DAYS = 1.0
RUNWAY_WARN_DAYS = 3.0


def bot_db_path(data_dir: str) -> str:
    """``DB_PATH`` wins (prod may anchor bot.db elsewhere); else the manifest layout."""
    configured = os.getenv("DB_PATH")
    if configured:
        return configured
    return os.path.join(data_dir, "database", "bot.db")


def _pct(v: List[float], p: float) -> Optional[float]:
    if not v:
        return None
    s = sorted(v)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def _median(v: List[float]) -> Optional[float]:
    return _pct(v, 0.5)


def read_usage(user_id: str, db: str, since_iso: str,
               until_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """The raw rows this section is computed from. ``timestamp`` is TEXT
    ``YYYY-MM-DD HH:MM:SS`` (UTC, sqlite ``CURRENT_TIMESTAMP``). ``until_iso``
    bounds the window from above (inclusive) so a slice such as "since the
    deploy" can be measured on its own instead of blended into the 24 h."""
    from core.status_snapshot import _rows
    sql = ("SELECT session_id, input_tokens, cached_tokens, output_tokens, api_cost_usd, metadata "
           "FROM usage_records WHERE user_id=? AND timestamp > ?")
    params: tuple = (str(user_id), since_iso)
    if until_iso:
        sql += " AND timestamp <= ?"
        params += (until_iso,)
    return _rows(db, sql + " ORDER BY id", params)


def truncation_event_count(user_id: str, data_dir: str, since_ts: float,
                           until_ts: Optional[float] = None) -> Optional[int]:
    """Output truncations in the window from the durable event log — the 057
    ``llm_output_truncated`` event the runner emits whether or not the retry is
    on. This is the honest source: the usage tracker writes no ``finish_reason``
    into ``usage_records.metadata``, so a metadata-only count reads ``0`` while
    a call sits at exactly the output cap (prod 2026-09-20 05:05Z, 8,192 tokens,
    retry fired, Economics said 0). ``None`` = the log is absent or unreadable
    (could not tell), never 0."""
    from core.event_log import telemetry_db_path
    from core.status_snapshot import _rows
    path = telemetry_db_path(data_dir)
    if not os.path.exists(path):
        return None
    sql = ("SELECT COUNT(*) AS n FROM telemetry_events "
           "WHERE kind='llm_output_truncated' AND user_id=? AND ts >= ?")
    params: tuple = (str(user_id), float(since_ts))
    if until_ts is not None:
        sql += " AND ts <= ?"
        params += (float(until_ts),)
    try:
        rows = _rows(path, sql, params)
    except Exception:
        return None
    try:
        return int(rows[0].get("n") or 0) if rows else 0
    except Exception:
        return None


def compute_economics(rows: List[Dict[str, Any]], window_sec: int,
                      truncation_events: Optional[int] = None) -> Dict[str, Any]:
    """Pure aggregation — the numbers every seat renders. ``None`` where the
    ledger cannot honestly answer (no calls, no durations). ``truncation_events``
    is the event-log count for the same window (``truncation_event_count``);
    when given it is the truncation figure and the metadata scan is only a floor."""
    n = len(rows)
    inp = sum(int(r.get("input_tokens") or 0) for r in rows)
    cached = sum(int(r.get("cached_tokens") or 0) for r in rows)
    out = sum(int(r.get("output_tokens") or 0) for r in rows)
    cost = sum(float(r.get("api_cost_usd") or 0.0) for r in rows)
    uncached_per_call: List[float] = []
    out_per_call: List[float] = []
    durations: List[float] = []
    truncated = 0
    timeouts = 0
    sessions: Dict[str, int] = {}
    first_hits = 0
    first_total = 0
    for r in rows:
        i = int(r.get("input_tokens") or 0)
        c = int(r.get("cached_tokens") or 0)
        uncached_per_call.append(float(max(0, i - c)))
        out_per_call.append(float(r.get("output_tokens") or 0))
        sid = str(r.get("session_id") or "")
        is_first = sid not in sessions
        sessions[sid] = sessions.get(sid, 0) + 1
        if is_first and i > 0:
            first_total += 1
            if c / i >= 0.7:
                first_hits += 1
        try:
            md = json.loads(r.get("metadata") or "{}") if isinstance(r.get("metadata"), str) else (r.get("metadata") or {})
        except Exception:
            md = {}
        d = md.get("duration_seconds")
        if isinstance(d, (int, float)):
            durations.append(float(d))
        if md.get("finish_reason") == "length" or md.get("truncated"):
            truncated += 1
        err = str(md.get("error") or "")
        if "timeout" in err.lower():
            timeouts += 1
    days = max(window_sec / 86400.0, 1e-9)
    return {
        "calls": n,
        "sessions": len(sessions),
        "input_tokens": inp,
        "cached_tokens": cached,
        "output_tokens": out,
        "cache_ratio": (cached / inp) if inp else None,
        "first_call_hit_ratio": (first_hits / first_total) if first_total else None,
        "uncached_per_call_median": _median(uncached_per_call),
        "output_per_call_median": _median(out_per_call),
        "cost_usd": cost,
        "cost_per_day_usd": cost / days if n else None,
        "latency_p50": _median(durations),
        "latency_p90": _pct(durations, 0.9),
        "truncated": max(truncated, truncation_events) if truncation_events is not None else truncated,
        "truncated_source": "event_log" if truncation_events is not None else "metadata",
        "timeouts": timeouts,
    }


def _k(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v / 1000:.1f}k"


def economics_lines(e: Dict[str, Any], label: str) -> List[str]:
    """The ONE render of a computed window — the status section and the
    ``scripts/econ_window.py`` slice print these same lines, so a target read
    off either seat is the same number. ``label`` names the window ("24h",
    "03:31→04:31")."""
    if not e["calls"]:
        return [f"llm usage: 0 calls in {label}"]
    cache = e["cache_ratio"]
    fch = e["first_call_hit_ratio"]
    lat = (f"latency p50 {e['latency_p50']:.0f}s · p90 {e['latency_p90']:.0f}s"
           if e["latency_p50"] is not None else "latency: not recorded")
    return [
        f"llm usage {label}: {e['calls']} calls · {e['sessions']} sessions · "
        f"${e['cost_usd']:.2f} (≈${e['cost_per_day_usd']:.2f}/day)",
        f"tokens: in {_k(e['input_tokens'])} ({(cache or 0) * 100:.0f}% cached) · out {_k(e['output_tokens'])} · "
        f"uncached/call median {_k(e['uncached_per_call_median'])} · out/call median {_k(e['output_per_call_median'])}",
        f"{lat} · first-call cache hit {('n/a' if fch is None else f'{fch * 100:.0f}%')} · "
        f"output truncations {e['truncated']}"
        f"{' (metadata only)' if e.get('truncated_source') == 'metadata' else ''} · "
        f"timeouts {e['timeouts']}",
    ]


def economics_section(user_id: str, data_dir: str, *, now: float,
                      window_sec: int = WINDOW_SEC_DEFAULT):
    from core.status_snapshot import STATE_UNAVAILABLE, Section
    import datetime as _dt
    sec = Section(name="economics", data={})
    db = bot_db_path(data_dir)
    if not os.path.exists(db):
        # A ledger that was never created is a fresh install with no model call
        # yet — an honest ok, like an absent avatar. An EXISTING ledger that
        # fails to read stays unavailable (the _guarded wrapper renders it).
        sec.lines.append("llm usage: no ledger yet (no model call recorded)")
        return sec
    since = _dt.datetime.fromtimestamp(now - window_sec, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = read_usage(user_id, db, since)  # raises → _guarded renders the reason
    e = compute_economics(rows, window_sec,
                          truncation_event_count(user_id, data_dir, now - window_sec, now))
    sec.data = e
    hours = max(1, int(window_sec // 3600))
    sec.lines.extend(economics_lines(e, f"{hours}h" if e["calls"] else f"the last {hours}h"))
    return sec


def runway_health(sec, balance_usd: Optional[float]):
    """Attach ``budget_runway`` when BOTH a read balance and a measured burn
    exist. Returns the item or None. Deliberately not called with a guessed
    balance."""
    from core.status_snapshot import SEVERITY_CRIT, SEVERITY_WARN, HealthItem
    per_day = (sec.data or {}).get("cost_per_day_usd")
    if balance_usd is None or not per_day or per_day <= 0:
        return None
    days = float(balance_usd) / float(per_day)
    hours = days * 24
    text = (f"budget runway ≈ {hours:.0f}h (${float(balance_usd):.2f} at ${per_day:.2f}/day)"
            if days < 2 else f"budget runway ≈ {days:.1f} days (${float(balance_usd):.2f} at ${per_day:.2f}/day)")
    sec.lines.append(text)
    if days >= RUNWAY_WARN_DAYS:
        return None
    item = HealthItem(
        key="budget_runway",
        severity=SEVERITY_CRIT if days < RUNWAY_CRIT_DAYS else SEVERITY_WARN,
        text=text,
        remedy="top up the provider balance, narrow the autonomous rigs (AUTONOMOUS_RIG_DEFAULT), "
               "or set AUTONOMOUS_CONTEXT_BUDGET_TOKENS / AUTONOMOUS_MAX_OUTPUT_TOKENS",
    )
    sec.health.append(item)
    return item
