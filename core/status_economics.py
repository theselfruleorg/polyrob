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
    sql = ("SELECT session_id, input_tokens, cached_tokens, output_tokens, api_cost_usd, metadata, "
           "timestamp FROM usage_records WHERE user_id=? AND timestamp > ?")
    params: tuple = (str(user_id), since_iso)
    if until_iso:
        sql += " AND timestamp <= ?"
        params += (until_iso,)
    return _rows(db, sql + " ORDER BY id", params)


def cost_window_usd(user_id: str, data_dir: str, *, now: float,
                    window_sec: int = WINDOW_SEC_DEFAULT) -> Optional[float]:
    """THE compute cost of the trailing window (080 WS-4) — the same
    ``usage_records.api_cost_usd`` sum the economics section renders, so the
    money section's runtime line and the economics line cannot disagree.
    ``0.0`` when no ledger exists yet (a fresh install made no model call);
    raises when an existing ledger cannot be read (the caller names it)."""
    import datetime as _dt
    db = bot_db_path(data_dir)
    if not os.path.exists(db):
        return 0.0
    since = _dt.datetime.fromtimestamp(now - window_sec, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    rows = read_usage(user_id, db, since)
    return round(sum(float(r.get("api_cost_usd") or 0.0) for r in rows), 6)


def run_cost_usd(session_id: str, data_dir: Optional[str] = None) -> Optional[float]:
    """The compute cost of ONE run (a goal run or a cron run) by its session id:
    the sum of ``usage_records.api_cost_usd`` written under that session (080
    WS-4). ``None`` = could not tell (no session id, no ledger, or the ledger
    did not read) — never a confident ``0``. A run that made no model call and
    has a readable ledger is a true ``0.0``."""
    from core.runtime_paths import data_dir_or_home
    from core.status_snapshot import _rows
    sid = str(session_id or "").strip()
    if not sid:
        return None
    db = bot_db_path(data_dir_or_home(data_dir))
    if not os.path.exists(db):
        return None
    try:
        rows = _rows(db, "SELECT COALESCE(SUM(api_cost_usd), 0) AS usd FROM usage_records "
                         "WHERE session_id=?", (sid,))
        return round(float((rows[0] or {}).get("usd") or 0.0), 6) if rows else 0.0
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("run_cost_usd(%s): %s", sid, e)
        return None


def run_usage_totals(session_id: str, data_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """ONE run's model use by its session id: ``{calls, input_tokens,
    cached_tokens, output_tokens, cost_usd}`` summed from ``usage_records``
    (2026-10-14 review D-4 — the ``cron_run`` row carries it so a run's cost
    and its outcome are one row). ``None`` = could not tell (no id, no ledger,
    a failed read) — never a confident zero."""
    from core.runtime_paths import data_dir_or_home
    from core.status_snapshot import _rows
    sid = str(session_id or "").strip()
    if not sid:
        return None
    db = bot_db_path(data_dir_or_home(data_dir))
    if not os.path.exists(db):
        return None
    try:
        rows = _rows(db, "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS i, "
                         "COALESCE(SUM(cached_tokens), 0) AS c, "
                         "COALESCE(SUM(output_tokens), 0) AS o, "
                         "COALESCE(SUM(api_cost_usd), 0) AS usd "
                         "FROM usage_records WHERE session_id=?", (sid,))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("run_usage_totals(%s): %s", sid, e)
        return None
    r = (rows or [{}])[0] or {}
    return {"calls": int(r.get("calls") or 0), "input_tokens": int(r.get("i") or 0),
            "cached_tokens": int(r.get("c") or 0), "output_tokens": int(r.get("o") or 0),
            "cost_usd": round(float(r.get("usd") or 0.0), 6)}


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
                      truncation_events: Optional[int] = None,
                      runs: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Pure aggregation — the numbers every seat renders. ``None`` where the
    ledger cannot honestly answer (no calls, no durations). ``truncation_events``
    is the event-log count for the same window (``truncation_event_count``);
    when given it is the truncation figure and the metadata scan is only a floor.

    2026-10-14 review D-11 / E-6 (owner decision D6): the window also splits
    ``by_model``, ``by_purpose`` and ``by_day`` (always — from the row), and
    ``by_regime``, ``by_job`` and ``$ per useful run`` when ``runs`` is given
    (``read_run_index``: session id -> the run's kind, job and useful flag).
    A row's own ``metadata.regime`` wins over the run index. ``cost_usd`` is
    the billed cost where the provider reported one (``api_cost_usd`` already
    carries it, F23); ``billed_share`` says how many rows that was."""
    n = len(rows)
    inp = sum(int(r.get("input_tokens") or 0) for r in rows)
    cached = sum(int(r.get("cached_tokens") or 0) for r in rows)
    # F17: cache WRITES, read from the row metadata (not a column) so this
    # section never depends on a schema the DB in front of it may predate.
    cache_write = 0
    cache_write_rows = 0
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
        cw = md.get("cache_creation_tokens")
        if isinstance(cw, (int, float)) and not isinstance(cw, bool):
            cache_write += int(cw)
            cache_write_rows += 1
        d = md.get("duration_seconds")
        if isinstance(d, (int, float)):
            durations.append(float(d))
        if md.get("finish_reason") == "length" or md.get("truncated"):
            truncated += 1
        err = str(md.get("error") or "")
        if "timeout" in err.lower():
            timeouts += 1
    days = max(window_sec / 86400.0, 1e-9)
    split = _splits(rows, runs)
    return {
        **split,
        "calls": n,
        "sessions": len(sessions),
        "input_tokens": inp,
        "cached_tokens": cached,
        "output_tokens": out,
        "cache_ratio": (cached / inp) if inp else None,
        # None = "no row in this window recorded the number", NEVER 0 — a cache
        # write nobody measured is not a cache write that did not happen. Rows
        # written before F17 carry no key at all.
        "cache_write_tokens": cache_write if cache_write_rows else None,
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


# -- the owner spend view (2026-10-14 review D-11 / E-6, owner decision D6) --

#: How a run kind (an ``episodes.kind`` or a usage row's ``regime``) reads on a
#: seat. One word per concept (``core/copy/glossary.py``).
KIND_WORDS = {"owner_chat": "chat", "chat": "chat", "cron": "scheduled jobs",
              "goal": "goals", "autonomous": "Rob's own initiative", "room": "groups",
              "correspondent": "people", "child": "helpers", "unattributed": "other"}
_RUN_KINDS = ("cron", "goal")


def _md(r: Dict[str, Any]) -> Dict[str, Any]:
    raw = r.get("metadata")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _bucket(acc: Dict[str, Dict[str, Any]], key: str, r: Dict[str, Any]) -> None:
    b = acc.setdefault(key, {"calls": 0, "cost_usd": 0.0, "input_tokens": 0,
                             "cached_tokens": 0})
    b["calls"] += 1
    b["cost_usd"] += float(r.get("api_cost_usd") or 0.0)
    b["input_tokens"] += int(r.get("input_tokens") or 0)
    b["cached_tokens"] += int(r.get("cached_tokens") or 0)


def _ranked(acc: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for key, b in acc.items():
        inp = b["input_tokens"]
        out.append({"key": key, **b, "cost_usd": round(b["cost_usd"], 6),
                    "cache_ratio": (b["cached_tokens"] / inp) if inp else None})
    return sorted(out, key=lambda x: (-x["cost_usd"], x["key"]))


def episode_useful(meta: Any) -> bool:
    """A run did something: its stored record names a send, an output or a
    tx. The episodic writer (``episodic_digest.record_run``) stores exactly the
    non-empty facts of ``did_something`` in ``meta``, so this reads the same
    predicate back from the store (core may not import the agents tier)."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except Exception:
            meta = {}
    return bool(isinstance(meta, dict) and (meta.get("sent") or meta.get("outputs")
                                            or meta.get("tx")))


def _splits(rows: List[Dict[str, Any]], runs: Optional[Dict[str, Dict[str, Any]]]
            ) -> Dict[str, Any]:
    by_model: Dict[str, Dict[str, Any]] = {}
    by_purpose: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    by_regime: Dict[str, Dict[str, Any]] = {}
    by_job: Dict[str, Dict[str, Any]] = {}
    run_cost: Dict[str, float] = {}
    billed = 0
    for r in rows:
        md = _md(r)
        if md.get("billed_cost_usd") is not None:
            billed += 1
        _bucket(by_model, str(md.get("model") or "unknown"), r)
        _bucket(by_purpose, str(md.get("purpose") or "unknown"), r)
        _bucket(by_day, str(r.get("timestamp") or "")[:10] or "unknown", r)
        sid = str(r.get("session_id") or "")
        run = (runs or {}).get(sid) if runs is not None else None
        regime = md.get("regime") or (run or {}).get("kind")
        if runs is not None or md.get("regime"):
            _bucket(by_regime, str(regime or "unattributed"), r)
        if run and run.get("job"):
            _bucket(by_job, str(run["job"]), r)
        if run and run.get("kind") in _RUN_KINDS:
            run_cost[sid] = run_cost.get(sid, 0.0) + float(r.get("api_cost_usd") or 0.0)
    out: Dict[str, Any] = {
        "by_model": _ranked(by_model), "by_purpose": _ranked(by_purpose),
        "by_day": sorted(_ranked(by_day), key=lambda x: x["key"]),
        "billed_share": (billed / len(rows)) if rows else None,
        "by_regime": _ranked(by_regime) if (runs is not None or by_regime) else None,
        "by_job": _ranked(by_job) if runs is not None else None,
        "runs": None, "useful_runs": None, "cost_per_useful_run_usd": None,
        "noop_run_cost_usd": None,
    }
    if runs is not None:
        useful = [sid for sid in run_cost if runs[sid].get("useful")]
        total = sum(run_cost.values())
        out.update(runs=len(run_cost), useful_runs=len(useful),
                   cost_per_useful_run_usd=(total / len(useful)) if useful else None,
                   noop_run_cost_usd=round(sum(v for k, v in run_cost.items()
                                               if not runs[k].get("useful")), 6))
    return out


def read_run_index(user_id: str, data_dir: str, since_ts: float) -> Optional[Dict[str, Dict[str, Any]]]:
    """``{session_id: {kind, job, useful}}`` from the episodes table (memory.db)
    for runs that ended in the window. ``None`` when the store does not exist
    (nothing to attribute); RAISES on a failed read (the caller names it)."""
    from core.runtime_paths import memory_db_path
    from core.status_snapshot import _rows
    from core.identity import normalize_user_id
    db = memory_db_path(data_dir)
    if not os.path.exists(db):
        return None
    rows = _rows(db, "SELECT session_id, kind, goal_id, meta FROM episodes "
                     "WHERE user_id=? AND ts >= ?",
                 (normalize_user_id(user_id), int(since_ts)))
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        meta = _md({"metadata": r.get("meta")})
        job = meta.get("job_id")
        out[str(r.get("session_id") or "")] = {
            "kind": str(r.get("kind") or ""),
            "job": (f"job:{job}" if job else
                    f"goal:{r['goal_id']}" if r.get("goal_id") else None),
            "useful": episode_useful(meta)}
    return out


def spend_view(user_id: str, data_dir: str, *, now: float, days: int = 7) -> Dict[str, Any]:
    """The ``/usage [days]`` view: :func:`compute_economics` over the last
    ``days`` with the run index. ``{"economics": …, "runs_error": str|None,
    "no_ledger": bool}``. RAISES when the usage ledger exists and cannot be
    read; an unreadable run index is NAMED (``runs_error``), never zero."""
    import datetime as _dt
    days = max(1, min(int(days), 90))
    window = days * 86400
    db = bot_db_path(data_dir)
    if not os.path.exists(db):
        return {"economics": compute_economics([], window), "runs_error": None,
                "no_ledger": True, "days": days}
    since = _dt.datetime.fromtimestamp(now - window, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    rows = read_usage(user_id, db, since)
    runs_error = None
    try:
        runs = read_run_index(user_id, data_dir, now - window)
    except Exception as e:  # noqa: BLE001 — named on the view
        runs, runs_error = None, f"{type(e).__name__}: {str(e)[:120]}"
    return {"economics": compute_economics(rows, window, runs=runs), "runs_error": runs_error,
            "no_ledger": False, "days": days}


def _money(v: Optional[float]) -> str:
    return "n/a" if v is None else (f"${v:.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}")


def _share(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "n/a"


def _label(key: str, names: Optional[Dict[str, str]]) -> str:
    if names and key in names:
        return names[key]
    kind, _, ident = key.partition(":")
    if kind in ("job", "goal") and ident:
        return f"{'scheduled job' if kind == 'job' else 'goal'} {ident[:8]}"
    return KIND_WORDS.get(key, key)


def spend_view_lines(view: Dict[str, Any], *, names: Optional[Dict[str, str]] = None,
                     top: int = 5) -> List[str]:
    """The ONE render of :func:`spend_view` — Telegram, the console and the
    terminal print these lines. ``names`` maps ``job:<id>``/``goal:<id>`` to a
    title (the seat resolves it; core does not read the job store)."""
    days = view.get("days") or 7
    e = view["economics"]
    period = "the last 24 h" if days == 1 else f"the last {days} days"
    if view.get("no_ledger"):
        return [f"AI cost, {period}: no records yet (no AI request was made)."]
    if not e["calls"]:
        return [f"AI cost, {period}: $0.00 — no AI request in the records."]
    total = e["cost_usd"]
    cache = e["cache_ratio"]
    billed = e.get("billed_share")
    out = [f"AI cost, {period}: {_money(total)} · {e['calls']} AI requests · "
           f"{(cache or 0) * 100:.0f}% of input from cache",
           ("cost source: the provider's bill" if billed and billed >= 0.99 else
            f"cost source: the provider's bill on {(billed or 0) * 100:.0f}% of requests, "
            "an estimate on the rest")]

    def _block(title: str, items, label=lambda k: k):
        if not items:
            return
        out.append(f"{title}:")
        for b in items[:top]:
            cr = b.get("cache_ratio")
            out.append(f"  {label(b['key'])}: {_money(b['cost_usd'])} ({_share(b['cost_usd'], total)}) "
                       f"· {b['calls']} requests"
                       + (f" · {cr * 100:.0f}% cached" if cr is not None else ""))
        if len(items) > top:
            rest = sum(b["cost_usd"] for b in items[top:])
            out.append(f"  … {len(items) - top} more: {_money(rest)}")

    if view.get("runs_error"):
        out.append(f"by kind of work, by job, per useful run: could not read the run "
                   f"records ({view['runs_error']}) — not zero")
    else:
        _block("by kind of work", e.get("by_regime"), lambda k: _label(k, names))
        _block("by scheduled job or goal", e.get("by_job"), lambda k: _label(k, names))
        if e.get("runs"):
            per = e.get("cost_per_useful_run_usd")
            out.append(f"runs: {e['runs']} scheduled-job and goal runs, {e['useful_runs']} did "
                       f"something · {_money(per) if per is not None else 'no useful run'} per "
                       f"useful run · {_money(e['noop_run_cost_usd'])} on runs that did nothing")
    _block("by model", e.get("by_model"))
    _block("by purpose", e.get("by_purpose"))
    if days > 1:
        _block("by day", e.get("by_day"))
    return out


def top_cost_line(view: Dict[str, Any], *, names: Optional[Dict[str, str]] = None) -> str:
    """ONE digest line: the biggest AI cost of the window and what it was."""
    e = view["economics"]
    if view.get("no_ledger") or not e["calls"]:
        return "Top AI cost: none (no AI request in the records)"
    total = e["cost_usd"]
    jobs = e.get("by_job") or []
    if jobs:
        j = jobs[0]
        what = f"{_label(j['key'], names)} {_money(j['cost_usd'])} ({_share(j['cost_usd'], total)})"
    elif view.get("runs_error"):
        what = "by job: could not read the run records"
    else:
        m = (e.get("by_model") or [{}])[0]
        what = f"model {m.get('key')} {_money(m.get('cost_usd'))}"
    per = e.get("cost_per_useful_run_usd")
    tail = f" · {_money(per)} per useful run" if per is not None else ""
    return f"Top AI cost: {what} of {_money(total)}{tail} — /usage for the split"


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
        f"tokens: in {_k(e['input_tokens'])} ({(cache or 0) * 100:.0f}% cached, "
        f"writes {_k(e.get('cache_write_tokens'))}) · out {_k(e['output_tokens'])} · "
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


def _economics_slot(ctx):
    """067 P5a: the ``economics`` slot (was ``status_snapshot._attach_economics``).
    The runway item needs a READ balance; the ledger carries one only when the
    caller asked for ``include_balances`` (the money slot, built first, hands
    its ledger on in ``ctx.ledger``) — otherwise the section still renders burn
    and the runway line is simply absent (never a guessed number). The runway
    item is added AFTER the guard, so it does not change the section state."""
    from core.status_snapshot import _guarded
    sec = _guarded("economics", economics_section, ctx.uid, ctx.data_dir,
                   now=ctx.now, window_sec=ctx.window_sec)
    ledger = ctx.ledger
    if sec.available and isinstance(ledger, dict):
        bal = (ledger.get("runtime") or {}).get("provider_balance_usd")
        try:
            runway_health(sec, bal)
        except Exception as e:  # never take the section down over the runway line
            sec.lines.append(f"budget runway: UNKNOWN ({type(e).__name__}: {e})")
    return sec


from core.status_sections import register_status_section  # noqa: E402

register_status_section("economics", _economics_slot)
