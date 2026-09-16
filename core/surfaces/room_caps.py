"""044 T11: bounded room traffic. One table in surfaces.db; every check is a
COUNT over a window, so restarts cannot reset a cap. Humans are never counted
by the bot-loop guard."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

from core.env import int_env
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS room_events (
    surface TEXT NOT NULL, chat_id TEXT NOT NULL, kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '', ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS room_events_idx ON room_events(surface, chat_id, kind, ts);
"""


def _cfg(name: str, default: int) -> int:
    return int_env(name, default)


class RoomCaps:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                execute_retry(db_path, stmt)

    def _policy(self, surface, chat_id):
        """This room's ``chat.*`` overlay (044 T17), or None.

        Read HERE rather than threaded in from the two call sites, because they
        live in different layers (``route_inbound`` and ``MessageRouter.publish``)
        and only one of them has a container. Fail-open to None = the env
        defaults, byte-identical to pre-T17.
        """
        try:
            from core.surfaces.chat_policy import load_for_chat
            return load_for_chat(os.path.dirname(self.db_path) or ".", surface, chat_id)
        except Exception as e:  # pragma: no cover - fail-open probe
            logger.debug("room caps: chat policy unreadable (%s)", e)
            return None

    @staticmethod
    def _int_or(room_value, name: str, default: int) -> int:
        """The ROOM's own number, else the operator env.

        ``room_value`` is None when the room said nothing — and a policy default
        that happens to equal this function's default is NOT the room saying
        something, which is why every caller gates on ``ChatPolicy.is_set``
        before reading the field. Without that, a deployment running
        ``GROUP_REPLY_CAP_PER_HOUR=50`` would be silently clamped back to the
        dataclass default of 20.
        """
        try:
            if room_value is not None:
                return int(room_value)
        except (TypeError, ValueError) as e:
            logger.debug("room caps: unusable per-room value %r for %s (%s)",
                         room_value, name, e)
        return _cfg(name, default)

    def _count(self, surface, chat_id, kind, since, actor=None) -> int:
        sql = "SELECT COUNT(*) FROM room_events WHERE surface=? AND chat_id=? AND kind=? AND ts>=?"
        params = [surface, str(chat_id), kind, since]
        if actor is not None:
            sql += " AND actor=?"
            params.append(actor)
        row = execute_retry(self.db_path, sql, tuple(params), fetch="one")
        return int(row[0]) if row else 0

    def _record(self, surface, chat_id, kind, actor, now):
        execute_retry(self.db_path,
                      "INSERT INTO room_events(surface,chat_id,kind,actor,ts) VALUES(?,?,?,?,?)",
                      (surface, str(chat_id), kind, actor or "", now))

    def may_trigger(self, surface, chat_id, sender_id, *, is_bot: bool,
                    now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        if is_bot:
            win = _cfg("GROUP_BOT_LOOP_WINDOW_SEC", 300)
            if self._count(surface, chat_id, "bot_trigger", now - win) >= _cfg("GROUP_BOT_LOOP_MAX", 20):
                return False, "bot loop guard: cooling down"
            cool = _cfg("GROUP_BOT_LOOP_COOLDOWN_SEC", 600)
            if self._count(surface, chat_id, "bot_loop_trip", now - cool) > 0:
                return False, "bot loop guard: cooling down"
        pol = self._policy(surface, chat_id)
        cd = self._int_or(
            pol.member_cooldown_sec if pol is not None and pol.is_set("member_cooldown_sec")
            else None, "GROUP_MEMBER_COOLDOWN_SEC", 20)
        if cd <= 0:
            return True, ""
        if self._count(surface, chat_id, "trigger", now - cd, actor=str(sender_id)) > 0:
            return False, f"member cooldown ({cd}s)"
        return True, ""

    def record_trigger(self, surface, chat_id, sender_id, *, is_bot: bool,
                       now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._record(surface, chat_id, "trigger", str(sender_id), now)
        if is_bot:
            self._record(surface, chat_id, "bot_trigger", str(sender_id), now)
            win = _cfg("GROUP_BOT_LOOP_WINDOW_SEC", 300)
            if self._count(surface, chat_id, "bot_trigger", now - win) >= _cfg("GROUP_BOT_LOOP_MAX", 20):
                self._record(surface, chat_id, "bot_loop_trip", "", now)

    def may_reply(self, surface, chat_id, *, now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        pol = self._policy(surface, chat_id)
        cap = self._int_or(
            pol.reply_cap_per_hour if pol is not None and pol.is_set("reply_cap_per_hour")
            else None, "GROUP_REPLY_CAP_PER_HOUR", 20)
        if self._count(surface, chat_id, "reply", now - 3600) >= cap:
            return False, f"room reply cap ({cap}/h) reached"
        return True, ""

    def record_reply(self, surface, chat_id, *, now: Optional[float] = None) -> None:
        self._record(surface, chat_id, "reply", "", time.time() if now is None else now)

    def replies_since(self, surface, chat_id, since: float) -> int:
        """044 T18: how many replies this room got since *since* (epoch
        seconds) — the public read the status section renders from. ``_count``
        stays private (it also takes a ``kind``/``actor`` an outside reader has
        no business choosing)."""
        return self._count(surface, chat_id, "reply", since)
