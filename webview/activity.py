"""Global activity stream — the `/activity` terminal's server side.

One normalized event shape for everything that happens on the instance:

    {id, ts, source, user_id, session_id, kind, summary, payload}

Sources (all cross-process safe — the webview is a separate process from the
agent in prod):

- ``feed``      — per-session ``feed/*.json`` files (rich agent telemetry),
                  pushed live by a single recursive ``watchfiles`` watcher.
- ``telemetry`` — the durable ``telemetry_events.db`` append-only log
                  (cron_run / self_wake / wallet_spend / tool_denied / ...).
- ``goal``      — ``goals.db::goal_events`` (id-cursor tail).
- ``skill``     — ``skill_usage.db::skill_install_audit`` (id-cursor tail).

Design notes: NO ``from __future__ import annotations`` (repo landmine);
flags via ``os.environ``/``core.env.bool_env`` (never ``BotConfig.get``);
everything fail-open — a missing DB / unreadable file degrades to silence,
never a 500 or a crashed watcher.
"""
import asyncio
import itertools
import json
import logging
import os
import sqlite3
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from core import event_kinds as ek
from webview import webgate

logger = logging.getLogger(__name__)

router = APIRouter()


def _templates() -> Jinja2Templates:
    """Same asset base as server.py/pages.py (packaged dir, repo fallback)."""
    try:
        from core.assets import webgate_asset_dir
        templates_dir = webgate_asset_dir() / "templates"
    except Exception:  # fail-open to the repo checkout
        templates_dir = Path(__file__).resolve().parent / "templates"
    return Jinja2Templates(directory=str(templates_dir))


_TEMPLATES = _templates()
_TEMPLATES.env.globals["console_display_name"] = webgate.console_display_name
_TEMPLATES.env.globals["branding"] = webgate.branding_config
# Posture default for the layout's tenant-nav block (P0-3) — same global as
# server.py's/pages.py's envs.
_TEMPLATES.env.globals["is_multitenant_posture"] = webgate.is_multitenant
try:
    from core.version import get_version
    _TEMPLATES.env.globals["get_version"] = get_version
except Exception:
    _TEMPLATES.env.globals["get_version"] = lambda: ""

# Feed kinds that are pure token-stream noise in a global terminal. They stay
# visible in the per-session view; the global stream drops them.
_SUPPRESSED_FEED_KINDS = {"streaming_output"}

_PAYLOAD_CAP_BYTES = 16000


def _snip(value: Any, n: int = 100) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[: n - 1] + "…" if len(text) > n else text


def _cap_payload(obj: Any) -> Any:
    """Bound payload size so one huge event can't bloat the buffer/wire."""
    try:
        dumped = json.dumps(obj, default=str)
    except Exception:
        return {"_truncated": True, "keys": []}
    if len(dumped) <= _PAYLOAD_CAP_BYTES:
        return obj
    keys = sorted(obj.keys()) if isinstance(obj, dict) else []
    return {"_truncated": True, "keys": keys, "preview": dumped[:2000]}


# Goal-title cache (P1-6): goal events carry only a goal_id; the title lives in
# goals.db. Bounded LRU, fail-open (a miss/error is just "no title"), and only
# successful lookups are cached so a transient DB error can't stick.
_GOAL_TITLE_CACHE: "OrderedDict[str, str]" = OrderedDict()
_GOAL_TITLE_CACHE_MAX = 256


def _goal_title(goal_id: Any) -> Optional[str]:
    if not goal_id:
        return None
    gid = str(goal_id)
    cached = _GOAL_TITLE_CACHE.get(gid)
    if cached is not None:
        _GOAL_TITLE_CACHE.move_to_end(gid)
        return cached
    db_path = os.path.join(_data_dir(), "goals.db")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT title FROM goals WHERE id=?", (gid,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    title = str(row[0]) if row and row[0] else None
    if title:
        _GOAL_TITLE_CACHE[gid] = title
        while len(_GOAL_TITLE_CACHE) > _GOAL_TITLE_CACHE_MAX:
            _GOAL_TITLE_CACHE.popitem(last=False)
    return title


def _ensure_goal_title(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill ``title`` from goals.db when a goal event carries only goal_id."""
    if isinstance(data, dict) and data.get("goal_id") and not data.get("title"):
        title = _goal_title(data.get("goal_id"))
        if title:
            data["title"] = title
    return data


def summarize(kind: str, data: Dict[str, Any]) -> str:
    """One terminal line of human summary per event kind. Defensive: every
    field is optional; unknown kinds fall back to the kind itself."""
    d = data if isinstance(data, dict) else {}
    if kind == "tool_execution":
        name = d.get("tool_name") or d.get("action") or d.get("name") or "?"
        ok = d.get("success", True)
        return f"tool {name} {'ok' if ok else 'FAILED'}"
    # 019 run-state span/wait events
    if kind == "tool_started":
        name = d.get("action_name") or d.get("tool_name") or "?"
        return f"→ {name} started"
    if kind == "llm_started":
        model = d.get("model_name") or d.get("provider") or "?"
        return f"thinking ({model})"
    if kind == "awaiting_approval":
        return f"⏸ awaiting approval: {d.get('action_name') or '?'}"
    if kind == "approval_resolved":
        waited = d.get("waited_sec")
        tail = f" after {waited:.0f}s" if isinstance(waited, (int, float)) and waited >= 1 else ""
        return f"approval {d.get('decision') or 'resolved'}: {d.get('action_name') or '?'}{tail}"
    if kind in ("compaction_started", "compaction_finished"):
        verb = "compacting" if kind == "compaction_started" else "compacted"
        return f"{verb} context ({d.get('mode') or '?'})"
    if kind == "retry_wait":
        delay = d.get("delay_sec")
        tail = f" {delay:.0f}s" if isinstance(delay, (int, float)) else ""
        return f"retrying ({d.get('reason') or '?'}){tail}"
    if kind in ("subagent_started", "subagent_finished"):
        verb = "started" if kind == "subagent_started" else ("finished ok" if d.get("ok") else "finished FAILED")
        return f"sub-agent {verb}: {_snip(d.get('goal_preview') or '', 60)}"
    if kind in ("delegation_dispatched", "delegation_completed"):
        verb = "dispatched" if kind == "delegation_dispatched" else (d.get("status") or "done")
        return f"delegation {d.get('delegation_id') or '?'} {verb}: {_snip(d.get('goal_preview') or '', 60)}"
    if kind == "provider_failure":
        return f"provider {d.get('failed_provider') or '?'} FAILED ({d.get('error_type') or '?'})"
    if kind == "provider_fallback_success":
        return f"provider fallback {d.get('original_provider') or '?'} → {d.get('fallback_provider') or '?'}"
    if kind == "llm_request":
        model = d.get("model_name") or d.get("model") or "?"
        tokens = d.get("token_count") or d.get("total_tokens") or 0
        cost = d.get("cost_estimate")
        tail = f" ${cost:.4f}" if isinstance(cost, (int, float)) and cost else ""
        return f"llm {model} {tokens}tk{tail}"
    if kind == "step":
        n = d.get("iteration", d.get("step", "?"))
        note = _snip(d.get("task_progress") or d.get("reasoning") or "", 80)
        return f"step {n}" + (f": {note}" if note else "")
    if kind == "session_start":
        return f"session started: {_snip(d.get('task') or d.get('task_description') or '', 90)}"
    if kind in ("session_completion", "task_complete"):
        return "session completed"
    if kind == "agent_end":
        return "agent ended"
    if kind == "agent_registration":
        return f"agent {d.get('agent_name') or d.get('name') or '?'} registered"
    if kind == "available_actions":
        actions = d.get("actions")
        count = len(actions) if isinstance(actions, (list, dict)) else d.get("count", "?")
        return f"actions registered ({count})"
    if kind == "status":
        return f"status → {d.get('status', '?')}"
    if kind == "error":
        return f"ERROR: {_snip(d.get('error_message') or d.get('message') or d.get('error') or '', 120)}"
    # Durable event-log kinds ride the core/event_kinds SSOT (T9) — a producer
    # rename now fails the contract test instead of silently emptying this feed.
    if kind == ek.CRON_RUN:
        return f"cron {d.get('job_id', '?')} → {d.get('outcome', '?')}"
    if kind == ek.SELF_WAKE:
        return f"self-wake → {d.get('outcome', '?')} ({_snip(d.get('reason'), 60)})"
    if kind == ek.WALLET_SPEND:
        return f"wallet spend ${d.get('amount_usd', '?')} @ {d.get('venue', '?')}"
    if kind == ek.TOOL_DENIED:
        return f"tool DENIED: {d.get('tool') or d.get('tool_name') or '?'}"
    if kind == ek.TOOL_TIMEOUT:
        return f"tool timeout: {d.get('tool') or d.get('tool_name') or '?'}"
    if kind == ek.AUTONOMY_TICK:
        return f"autonomy tick {d.get('loop', '?')} alive={d.get('alive', '?')}"
    if kind == ek.OWNER_NOTICE:
        return f"owner notice: {_snip(d.get('message') or d.get('text') or '', 100)}"
    if kind == "skill_install":
        return f"skill installed: {d.get('name', '?')} (by {d.get('approver') or d.get('source') or '?'})"
    if kind.startswith("goal_"):
        # goal_run rides outcome (started/done/failed/blocked); goal-board
        # events carry the verb in the kind (created/claimed/succeeded/...).
        verb = kind[len("goal_"):]
        outcome = d.get("outcome") or d.get("status")
        if outcome:
            verb = f"{verb} {outcome}" if verb == "run" else f"{verb} → {outcome}"
        title = _snip(d.get("title") or "", 70)
        gid = _snip(d.get("goal_id") or "?", 12)
        line = f"goal {verb}: {title} ({gid[:8]})" if title else f"goal {gid[:8]} {verb}"
        reason = d.get("reason") or d.get("error")
        if reason and str(outcome or "") in ("failed", "blocked", "gave_up"):
            line += f" — {_snip(reason, 80)}"
        return line
    return kind


def normalize_feed_event(user_id: str, session_id: str, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one per-session feed file's JSON into the activity shape.

    Handles both writer shapes: telemetry service files ``{..., _seq, _ts_ms,
    _id}`` and SessionManager files ``{timestamp, type, data}``. Returns None
    for suppressed kinds.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type") or raw.get("event_type") or "event"
    if kind in _SUPPRESSED_FEED_KINDS:
        return None
    if raw.get("_ts_ms"):
        ts = float(raw["_ts_ms"]) / 1000.0
    else:
        try:
            ts = float(raw.get("timestamp") or 0.0) or time.time()
        except (TypeError, ValueError):
            ts = time.time()
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    ev_id = raw.get("_id") or f"feed:{session_id}:{raw.get('_seq') or int(ts * 1000)}"
    return {
        "id": ev_id,
        "ts": ts,
        "source": "feed",
        "user_id": user_id or "",
        "session_id": session_id or "",
        "kind": kind,
        "summary": summarize(kind, data),
        "payload": _cap_payload(raw),
    }


def _parse_json_field(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def normalize_db_event(source: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one SQLite tail row (telemetry / goal / skill) into the
    activity shape."""
    if source == "goal":
        payload = _parse_json_field(row.get("payload"))
        payload.setdefault("goal_id", row.get("goal_id"))
        _ensure_goal_title(payload)
        kind = f"goal_{row.get('kind', 'event')}"
        return {
            "id": f"goal:{row.get('id')}",
            "ts": float(row.get("created_at") or 0.0),
            "source": "goal",
            "user_id": str(payload.get("user_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "kind": kind,
            "summary": summarize(kind, payload),
            "payload": _cap_payload(payload),
        }
    if source == "skill":
        payload = {
            "name": row.get("name"),
            "source": row.get("source"),
            "resolved_sha": row.get("resolved_sha"),
            "approver": row.get("approver"),
        }
        return {
            "id": f"skill:{row.get('id')}",
            "ts": float(row.get("ts") or 0.0),
            "source": "skill",
            "user_id": str(row.get("user_id") or ""),
            "session_id": "",
            "kind": "skill_install",
            "summary": summarize("skill_install", payload),
            "payload": payload,
        }
    # default: durable telemetry event log row
    attrs = _parse_json_field(row.get("attrs"))
    kind = str(row.get("kind") or "event")
    if kind.startswith("goal"):
        _ensure_goal_title(attrs)
    return {
        "id": f"telemetry:{row.get('id')}",
        "ts": float(row.get("ts") or 0.0),
        "source": "telemetry",
        "user_id": str(row.get("user_id") or ""),
        "session_id": str(row.get("session_id") or ""),
        "kind": kind,
        "summary": summarize(kind, attrs),
        "payload": _cap_payload(attrs),
    }


class SqliteTail:
    """Id-cursor tail over an append-only SQLite table (read-only, WAL-safe).

    ``prime()`` sets the cursor to the current MAX(id) so a fresh webview
    never floods the stream with history; ``poll()`` returns only rows that
    arrived since. A missing DB/table is silence, never an error — the tail
    starts delivering when the file appears.
    """

    def __init__(self, db_path: str, table: str, id_col: str = "id"):
        self.db_path = str(db_path)
        self.table = table
        self.id_col = id_col
        self.cursor = 0

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def prime(self) -> None:
        try:
            rows = self._query(f"SELECT MAX({self.id_col}) AS m FROM {self.table}")
            self.cursor = int(rows[0]["m"] or 0)
        except Exception:
            self.cursor = 0

    def poll(self, limit: int = 500) -> List[Dict[str, Any]]:
        try:
            rows = self._query(
                f"SELECT * FROM {self.table} WHERE {self.id_col} > ? "
                f"ORDER BY {self.id_col} ASC LIMIT ?",
                (self.cursor, limit),
            )
        except Exception:
            return []
        if rows:
            self.cursor = int(rows[-1][self.id_col])
        return rows


def _data_dir() -> str:
    """Data home for goals.db/skill_usage.db — same resolution as pages.py.

    Delegates to :func:`webview.webgate.data_dir` (the wrapper over the ONE
    core seam ``core.runtime_paths.resolve_data_home``; I-9, deduped
    2026-07-12). Keep the local name: `activity_db_sources` and tests use it."""
    return webgate.data_dir()


def _sessions_data_root() -> Optional[str]:
    """The PathManager session root (feed dirs + telemetry_events.db)."""
    try:
        from agents.task.path import pm
        return str(pm().data_root)
    except Exception:
        return None


def activity_db_sources() -> List[Tuple[str, SqliteTail]]:
    """The three SQLite tails feeding the system half of the stream."""
    root = _sessions_data_root()
    tel = (
        os.path.join(root, "telemetry_events.db")
        if root
        else os.path.join(_data_dir(), "telemetry_events.db")
    )
    return [
        ("telemetry", SqliteTail(tel, "telemetry_events")),
        ("goal", SqliteTail(os.path.join(_data_dir(), "goals.db"), "goal_events")),
        ("skill", SqliteTail(os.path.join(_data_dir(), "skill_usage.db"), "skill_install_audit")),
    ]


def feed_path_info(path: str, data_root: str) -> Optional[Tuple[str, str]]:
    """Derive ``(user_id, session_id)`` from a feed file path, or None.

    Accepts exactly ``{data_root}/{user}/{session}/feed/*.json``.
    """
    try:
        rel = Path(path).resolve().relative_to(Path(data_root).resolve())
    except Exception:
        return None
    parts = rel.parts
    if len(parts) != 4 or parts[2] != "feed" or not parts[3].endswith(".json"):
        return None
    return parts[0], parts[1]


class ActivityHub:
    """In-process fan-in for normalized activity events.

    Ring-buffers the last 1000 events for reconnect snapshots/backfill and
    owns the watcher + tail loop lifecycle (started lazily on first
    ``join_activity``, stopped when the activity room empties).
    """

    def __init__(self):
        self.buffer: deque = deque(maxlen=1000)
        self._counter = itertools.count(1)
        self._sio = None
        self._tasks: List[asyncio.Task] = []
        self._sources: List[Tuple[str, SqliteTail]] = []
        self._seen_files: deque = deque(maxlen=2048)
        self._seen_set: set = set()
        self.started = False

    def record(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        if not ev.get("id"):
            ev["id"] = f"act:{next(self._counter)}"
        self.buffer.append(ev)
        return ev

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self.buffer)[-limit:]

    # --- lifecycle --------------------------------------------------------- #

    def start(self, sio) -> None:
        """Start the tail loop + feed watcher (idempotent; needs a running loop)."""
        if self.started:
            return
        self.started = True
        self._sio = sio
        self._sources = activity_db_sources()
        for _, tail in self._sources:
            tail.prime()
        self._tasks = [
            asyncio.create_task(self._tail_loop()),
            asyncio.create_task(self._run_watcher()),
        ]
        logger.info("activity hub started (tails + feed watcher)")

    def stop(self) -> None:
        """Cancel the loops (sync contexts / backstop). Prefer ``aclose()``
        from async code so the cancelled tasks are actually awaited."""
        self.started = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        logger.info("activity hub stopped")

    async def aclose(self) -> None:
        """Cancel AND drain the loops — no pending-task warnings at loop close."""
        self.started = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("activity hub stopped")

    async def _emit(self, ev: Dict[str, Any]) -> None:
        self.record(ev)
        if self._sio is not None:
            try:
                await self._sio.emit("activity_event", ev, room="activity")
            except Exception as exc:  # emit failure must never kill the loops
                logger.debug(f"activity emit failed: {exc}")

    async def _tail_loop(self) -> None:
        try:
            interval = float(os.environ.get("WEBVIEW_ACTIVITY_TAIL_SEC", "2.0") or 2.0)
        except (TypeError, ValueError):
            interval = 2.0
        while True:
            try:
                for source, tail in self._sources:
                    for row in tail.poll():
                        await self._emit(normalize_db_event(source, row))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"activity tail error: {exc}")
            await asyncio.sleep(interval)

    async def _run_watcher(self) -> None:
        try:
            from watchfiles import Change, awatch
        except Exception:
            logger.warning("watchfiles unavailable — feed half of activity stream disabled")
            return
        root = _sessions_data_root()
        if not root or not os.path.isdir(root):
            logger.warning("activity watcher: no session data root — feed stream disabled")
            return

        def _wants(change, path: str) -> bool:
            return path.endswith(".json") and (os.sep + "feed" + os.sep) in path

        try:
            async for changes in awatch(root, watch_filter=_wants):
                for change, path in changes:
                    if change not in (Change.added, Change.modified):
                        continue
                    if path in self._seen_set:
                        continue  # feed files are write-once; first event wins
                    self._seen_set.add(path)
                    self._seen_files.append(path)
                    if len(self._seen_files) == self._seen_files.maxlen:
                        oldest = self._seen_files.popleft()
                        self._seen_set.discard(oldest)
                    info = feed_path_info(path, root)
                    if not info:
                        continue
                    ev = self._read_feed_file(path, info[0], info[1])
                    if ev:
                        await self._emit(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"activity feed watcher stopped: {exc}")

    def _read_feed_file(self, path: str, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return None
        return normalize_feed_event(user_id, session_id, raw)


_hub: Optional[ActivityHub] = None


def get_hub() -> ActivityHub:
    global _hub
    if _hub is None:
        _hub = ActivityHub()
    return _hub


# --- access gate + HTTP surface --------------------------------------------- #

def _require_activity_access(request: Request) -> None:
    """The global stream is inherently cross-tenant, so:

    - flag off → 404 (feature not there);
    - local → open (loopback operator IS the owner);
    - own_ops → the auth middleware already required the single owner's
      cookie for any non-public path, which /activity is — nothing extra;
    - multitenant → admin tier / is_admin / the instance owner ONLY. A plain
      authenticated tenant is refused: this page shows everyone's activity.
    """
    if not webgate.activity_enabled():
        raise HTTPException(status_code=404, detail="Activity stream disabled")
    if not webgate.requires_owner_login():
        return
    if webgate.is_multitenant():
        state = getattr(request, "state", None)
        user_id = getattr(state, "user_id", None)
        tier = getattr(state, "tier", None)
        is_admin = bool(getattr(state, "is_admin", False))
        if is_admin or tier == "admin" or (user_id and user_id == webgate.local_owner_id()):
            return
        raise HTTPException(status_code=403, detail="Owner/admin access required")
    return  # own_ops: single-owner model (H2b)


def _recent_feed_events(per_session: int = 20, sessions: int = 3) -> List[Dict[str, Any]]:
    """Cold-start seed: the last few feed events of the most recent sessions."""
    root = _sessions_data_root()
    if not root or not os.path.isdir(root):
        return []
    candidates = []
    try:
        with os.scandir(root) as users:
            for user_entry in users:
                if not user_entry.is_dir():
                    continue
                try:
                    with os.scandir(user_entry.path) as session_dirs:
                        for sess_entry in session_dirs:
                            if not sess_entry.is_dir():
                                continue
                            feed = os.path.join(sess_entry.path, "feed")
                            if os.path.isdir(feed):
                                candidates.append(
                                    (os.path.getmtime(feed), user_entry.name, sess_entry.name, feed)
                                )
                except OSError:
                    continue
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for _, user_id, session_id, feed in sorted(candidates, reverse=True)[:sessions]:
        try:
            names = sorted(n for n in os.listdir(feed) if n.endswith(".json"))[-per_session:]
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(feed, name), "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception:
                continue
            ev = normalize_feed_event(user_id, session_id, raw)
            if ev:
                out.append(ev)
    return out


def _cold_backfill(limit: int) -> List[Dict[str, Any]]:
    """Backfill when the hub buffer is cold: recent DB rows + recent feeds."""
    events: List[Dict[str, Any]] = []
    for source, tail in activity_db_sources():
        try:
            rows = tail._query(
                f"SELECT * FROM {tail.table} ORDER BY {tail.id_col} DESC LIMIT ?",
                (min(limit, 100),),
            )
        except Exception:
            continue
        events.extend(normalize_db_event(source, row) for row in reversed(rows))
    events.extend(_recent_feed_events())
    events.sort(key=lambda ev: ev.get("ts", 0.0))
    return events[-limit:]


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> Any:
    _require_activity_access(request)
    return _TEMPLATES.TemplateResponse(
        "activity.html",
        {"request": request, "read_only": webgate.read_only()},
    )


@router.get("/api/activity/backfill")
async def activity_backfill(request: Request, limit: int = 200) -> JSONResponse:
    _require_activity_access(request)
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    hub = get_hub()
    events = hub.recent(limit)
    if not events:
        events = _cold_backfill(limit)
    return JSONResponse({"events": events})
