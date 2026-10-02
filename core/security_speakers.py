"""045 lane 4 — cost and abuse per speaker.

Three questions, all answered with GROUP BY over stores that already exist:

- **volume** — which chats and which senders produced the most inbound traffic
  (``inbound_routed`` + ``access_denied`` rows in ``telemetry_events.db``,
  lane 1's attribution: ``attrs.surface``/``attrs.chat_id``/``attrs.sender``);
- **spend** — which chats cost the most model money. ``usage_records``
  (``bot.db``) is keyed by ``session_id``; ``session_chat_map``
  (``surfaces.db``) binds each chat's ``session_key`` to its ``session_id``.
  The join runs as two GROUP BY reads (sqlite cannot join across two files
  without ATTACH, and a read must never open a store for write);
- **trips** — which limiters tripped, on which keys (``rate_limited`` rows,
  emitted by ``core/rate_limit.py::report_trip``).

⚠️ Spend is per CHAT, not per member. A group room is one chat-scoped session
that every member shares, so one member's share of its cost is not recorded
anywhere; a DM chat is one member by construction. The rollup says this rather
than dividing a room's cost among its members.

⚠️ Tenancy follows lane 1: perimeter rows carry the DEPLOYMENT tenant, and the
spend join covers the sessions bound in this deployment's ``surfaces.db``.

Layering: core-only reads through ``core.sqlite_util`` (045 §10 invariant 8).
An absent store is ``unavailable(<reason>)`` — never a zero.
"""
import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_TOP_N = 5


@dataclass
class SpeakerRollup:
    top_chats_by_volume: List[Tuple[str, int]] = field(default_factory=list)
    top_chats_by_spend: List[Tuple[str, float]] = field(default_factory=list)
    top_limiters: List[Tuple[str, int]] = field(default_factory=list)
    top_limited_keys: List[Tuple[str, int]] = field(default_factory=list)
    #: "" when the spend join read both stores; else the reason it could not.
    spend_unavailable: str = ""


def _rows(db_path: str, sql: str, params: tuple) -> List[dict]:
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(
            f"{os.path.basename(db_path or '?')} not found at {db_path}")
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(db_path, sql, params, fetch="all") or [])]


def _chat_label(surface: Optional[str], chat_id: Optional[str]) -> str:
    return f"{surface or '?'}:{chat_id or '?'}"


def top_chats_by_volume(tel_db: str, user_id: str, since_ts: float,
                        kinds: Tuple[str, ...]) -> List[Tuple[str, int]]:
    placeholders = ",".join("?" for _ in kinds)
    rows = _rows(
        tel_db,
        "SELECT json_extract(attrs, '$.surface') AS s, "
        "json_extract(attrs, '$.chat_id') AS c, COUNT(*) AS n "
        f"FROM telemetry_events WHERE ts >= ? AND user_id = ? "
        f"AND kind IN ({placeholders}) AND c IS NOT NULL "
        "GROUP BY s, c ORDER BY n DESC, s ASC, c ASC LIMIT ?",
        (float(since_ts), str(user_id), *kinds, _TOP_N))
    return [(_chat_label(r["s"], r["c"]), int(r["n"])) for r in rows]


def _top_trip_attr(tel_db: str, user_id: str, since_ts: float,
                   attr: str) -> List[Tuple[str, int]]:
    from core.event_kinds import RATE_LIMITED
    rows = _rows(
        tel_db,
        "SELECT json_extract(attrs, ?) AS v, COUNT(*) AS n FROM telemetry_events "
        "WHERE ts >= ? AND user_id = ? AND kind = ? AND v IS NOT NULL "
        "GROUP BY v ORDER BY n DESC, v ASC LIMIT ?",
        (f"$.{attr}", float(since_ts), str(user_id), RATE_LIMITED, _TOP_N))
    return [(str(r["v"]), int(r["n"])) for r in rows]


def top_chats_by_spend(data_dir: str, since_ts: float) -> List[Tuple[str, float]]:
    """Per-chat model spend over the window. Raises when either store is
    absent or unreadable, so the caller can name the reason."""
    from core.status_economics import bot_db_path
    surfaces_db = os.path.join(str(data_dir), "surfaces.db")
    chats = _rows(
        surfaces_db,
        "SELECT session_id, surface_id, chat_id FROM session_chat_map "
        "WHERE session_id IS NOT NULL AND session_id != '' "
        "GROUP BY session_id", ())
    by_session: Dict[str, str] = {
        str(r["session_id"]): _chat_label(r["surface_id"], r["chat_id"]) for r in chats}
    if not by_session:
        return []
    since = _dt.datetime.fromtimestamp(
        float(since_ts), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    spend = _rows(
        bot_db_path(str(data_dir)),
        "SELECT session_id, COALESCE(SUM(api_cost_usd), 0) AS usd "
        "FROM usage_records WHERE timestamp >= ? GROUP BY session_id", (since,))
    totals: Dict[str, float] = {}
    for r in spend:
        label = by_session.get(str(r["session_id"]))
        if label is not None:
            totals[label] = totals.get(label, 0.0) + float(r["usd"] or 0.0)
    ranked = sorted(((k, round(v, 6)) for k, v in totals.items() if v > 0),
                    key=lambda kv: (-kv[1], kv[0]))
    return ranked[:_TOP_N]


def build_speaker_rollup(tel_db: str, user_id: str, *, data_dir: str,
                         since_ts: float) -> SpeakerRollup:
    """Volume and trips raise on an absent telemetry store (the caller's
    rollup already raises there). Spend degrades on its own: a missing
    ``bot.db`` or ``surfaces.db`` fills ``spend_unavailable`` and leaves the
    other lanes readable."""
    from core.event_kinds import ACCESS_DENIED, INBOUND_ROUTED
    r = SpeakerRollup(
        top_chats_by_volume=top_chats_by_volume(
            tel_db, user_id, since_ts, (INBOUND_ROUTED, ACCESS_DENIED)),
        top_limiters=_top_trip_attr(tel_db, user_id, since_ts, "limiter"),
        top_limited_keys=_top_trip_attr(tel_db, user_id, since_ts, "key"),
    )
    try:
        r.top_chats_by_spend = top_chats_by_spend(data_dir, since_ts)
    except Exception as e:
        r.spend_unavailable = f"{type(e).__name__}: {str(e)[:120]}"
    return r
