"""044 T17: per-chat behaviour — ONE TOML overlay per ``(surface, chat)``.

A room is not a tenant. Its mode, its name, its standing instructions and its
caps belong to the ROOM, not to the owner's private preferences, so they live in
their own file under the owner tenant:
``identity/{instance}/user_{uid}/chats/{surface}_{chat_id}.toml``.

They are validated by the SAME prefs machinery that validates a tenant
preference (``core/prefs.py``'s ``CHAT_PREF_SCHEMA`` + ``validate_pref``) —
one validator, one vocabulary, one error sentence. What differs is only WHERE
the value is stored and WHO reads it.

``chat.mode`` replaces the ``GROUP_REQUIRE_MENTION`` gate. Being ADDRESSED (an
@mention, a reply to the agent, or a ``chat.wake_words`` hit) is required at
every rung but ``active`` — the owner is NOT exempt:

- ``mention`` (default) — answer an addressed line from anyone.
- ``active``            — answer every message in the room.
- ``listen``            — read everything, answer an ADDRESSED owner or room
                          admin only.
- ``off``               — never answer, not even the owner (he changes the mode
                          from a seat, not by shouting into the room).

``GROUP_REQUIRE_MENTION=false`` is honoured for one release as an alias for
``chat.mode=active``.

Pure core: ``core.prefs`` at module top (both core), nothing from
agents/surfaces/tools.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any, Optional, Tuple

from core.prefs import (_render_toml, _threat_scan_pref_value,
                        chat_preferences_path as _chat_preferences_path,
                        validate_pref)

logger = logging.getLogger(__name__)

MODES = ("mention", "active", "listen", "off")

#: path -> (mtime_ns, ChatPolicy). Mirrors ``core.prefs._CACHE``: the dispatcher
#: and the room caps each read the policy on the inbound path, and a room's file
#: changes only when an owner writes it.
_CACHE: dict = {}



@dataclass(frozen=True)
class ChatPolicy:
    """What one room is configured to do. Every field has a working default, so
    a room with no file at all behaves exactly as a room did before 044 T17."""

    mode: str = "mention"
    name: str = ""
    persona: str = ""
    tone: str = ""
    verbosity: str = ""
    language: str = ""
    instructions: str = ""
    wake_words: Tuple[str, ...] = ()
    tool_deny: Tuple[str, ...] = ()
    member_verbs: Tuple[str, ...] = ("help",)
    reply_cap_per_hour: int = 20
    member_cooldown_sec: int = 20
    context_lines: int = 30
    quiet_hours: str = ""
    mute_until: float = 0.0
    thread_scope: str = "topic"
    reply_mode: str = "first"

    # --- 046: paid room actions. FLAT names on purpose — `load` maps
    # `chat.<field>` straight onto a dataclass field, so a dotted key would be
    # written and then silently dropped.
    paid_enabled: bool = False
    paid_asset: str = ""
    paid_mute_usd: float = 0.0
    paid_mute_min_usd: float = 0.0
    paid_mute_max_usd: float = 0.0
    paid_mute_max_duration: str = "24h"
    paid_unmute_usd: float = 0.0
    paid_ban_usd: float = 0.0
    paid_ban_min_usd: float = 0.0
    paid_ban_max_usd: float = 0.0
    paid_ban_max_duration: str = "24h"
    paid_unban_usd: float = 0.0
    #: 046b — the price in TOKEN units, as a STRING parsed with Decimal.
    #: ⚠️ Set one of these and the USD price stops deciding the AMOUNT: it
    #: stays the owner's declared value (ledger, X402_INVOICE_MAX_USD, owner
    #: reporting) while the token figure IS what the payer sends. That is what
    #: removes the price oracle — and therefore the pool screen — from the
    #: path, so a room can sell in a token whose market is too young to quote.
    #: String, not float: 18 decimals of a memecoin do not survive a float.
    paid_mute_units: str = ""
    paid_ban_units: str = ""
    paid_unmute_units: str = ""
    paid_unban_units: str = ""
    paid_max_per_payer_day: int = 3
    paid_max_per_target_day: int = 2
    paid_offer_ttl: str = "30m"

    #: Field names the owner actually WROTE for this room. A default that
    #: happens to equal the operator's env value is not the same thing as a
    #: choice: without this, ``RoomCaps`` could not tell "this room caps replies
    #: at 20" from "nobody said", and would have overridden
    #: ``GROUP_REPLY_CAP_PER_HOUR=50`` with the dataclass default.
    explicit: frozenset = frozenset()

    def is_set(self, field: str) -> bool:
        return field in self.explicit

    @classmethod
    def defaults(cls) -> "ChatPolicy":
        mode = (os.getenv("GROUP_DEFAULT_MODE") or "mention").strip().lower()
        if mode not in MODES:
            mode = "mention"
        # One-release alias: GROUP_REQUIRE_MENTION=false meant "respond to
        # everything in an allowlisted room", which is exactly `active`.
        from core.env import bool_env
        if (bool_env("GROUP_REQUIRE_MENTION", True) is False
                and not (os.getenv("GROUP_DEFAULT_MODE") or "").strip()):
            mode = "active"
        return cls(mode=mode)

    def replace(self, **kw) -> "ChatPolicy":
        return _dc_replace(self, **kw)

    def with_mode(self, mode: str) -> "ChatPolicy":
        return _dc_replace(self, mode=mode)


#: Fields whose TOML value is a list and whose dataclass field is a tuple.
_TUPLE_FIELDS = ("wake_words", "tool_deny", "member_verbs")


def chat_preferences_path(home_dir, owner_uid, surface, chat_id,
                          instance_id=None) -> Optional[Path]:
    """The overlay file for one room, or None for an unsafe/empty tenant."""
    return _chat_preferences_path(home_dir, owner_uid, surface, chat_id, instance_id)


def _flatten(nested: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in (nested or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _read_flat(path: Path) -> dict:
    import tomllib
    with open(path, "rb") as fh:
        return _flatten(tomllib.load(fh))


def load(home_dir, owner_uid, surface, chat_id, *,
         instance_id: Optional[str] = None) -> ChatPolicy:
    """The policy for one room. A missing/unreadable file reads as DEFAULTS.

    ``load`` never trusts the file: every value goes back through
    ``validate_pref``, so a hand-edited entry the writer would have refused is
    dropped rather than silently applied. That matters most for ``mode`` — a
    typo must not be able to turn a room off.
    """
    base = ChatPolicy.defaults()
    path = chat_preferences_path(home_dir, owner_uid, surface, chat_id, instance_id)
    if path is None or not path.is_file():
        return base
    cache_key = str(path)
    try:
        mtime = path.stat().st_mtime_ns
        cached = _CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
        flat = _read_flat(path)
    except Exception as e:
        logger.warning("chat policy unreadable (%s) — using defaults: %s", path, e)
        return base
    kw: dict = {}
    for key, value in flat.items():
        if not key.startswith("chat."):
            continue
        field = key.split(".", 1)[1]
        if field not in ChatPolicy.__dataclass_fields__:
            continue
        ok, coerced, err = validate_pref(key, value)
        if not ok:
            logger.warning("chat policy %s: dropping %s (%s)", path.name, key, err)
            continue
        kw[field] = tuple(coerced) if field in _TUPLE_FIELDS else coerced
    policy = _dc_replace(base, explicit=frozenset(kw), **kw)
    if policy.mode not in MODES:
        policy = _dc_replace(policy, mode=base.mode)
    _CACHE[cache_key] = (mtime, policy)
    return policy


def load_for_chat(data_dir, surface, chat_id) -> ChatPolicy:
    """``load`` for a call site that has a data dir and a chat but no tenant.

    The overlay lives under the OWNER tenant because the owner is who configures
    a room — the ONE owner-tenant resolver, the same one both writers use
    (`surfaces/telegram/group_ops.py::_owner_uid`,
    `cli/commands/owner.py::_group_owner_uid`). A deployment whose tenant cannot
    be resolved at all has no overlay to read and gets the defaults — never an
    error.
    """
    try:
        from core.instance import resolve_owner_user_id
        owner = resolve_owner_user_id()
    except Exception as e:
        logger.debug("chat policy: owner principal unresolved (%s)", e)
        owner = None
    if not owner:
        return ChatPolicy.defaults()
    return load(data_dir, owner, surface, chat_id)


def set(home_dir, owner_uid, surface, chat_id, key: str, value: Any, *,
        instance_id: Optional[str] = None) -> Tuple[bool, str]:
    """Validate + upsert ONE ``chat.*`` key; atomic temp+replace.

    Returns ``(ok, message)``; on refusal the message is the sentence a seat
    shows the owner, including the valid vocabulary.
    """
    if not str(key).startswith("chat."):
        return False, f"{key}: not a chat.* key (a room takes chat.* settings only)"
    ok, coerced, err = validate_pref(key, value)
    if not ok:
        return False, err
    from core.prefs import _THREAT_SCANNED_PREF_KEYS
    if key in _THREAT_SCANNED_PREF_KEYS:
        clean, why = _threat_scan_pref_value(key, coerced)
        if not clean:
            return False, why
    if key == "chat.wake_words":
        # A wake word is compiled as a regex on the inbound path. A pattern that
        # cannot compile must be refused HERE, not silently skipped every turn.
        for word in coerced:
            try:
                re.compile(str(word))
            except re.error as e:
                return False, f"chat.wake_words: bad pattern {word!r}: {e}"
    path = chat_preferences_path(home_dir, owner_uid, surface, chat_id, instance_id)
    if path is None:
        return False, "empty or unsafe owner id refused (tenant scope)"
    current: dict = {}
    if path.is_file():
        try:
            current = _read_flat(path)
        except Exception as e:
            # Never let an unreadable file silently erase the rest of the room's
            # settings: refuse the write and say so.
            return False, f"{path.name} is unreadable ({e}) — fix or remove it first"
    current[key] = coerced
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(_render_toml(current), encoding="utf-8")
        os.replace(str(tmp), str(path))
        _CACHE.pop(str(path), None)
    except Exception as e:
        logger.error("chat policy write failed (%s): %s", path, e, exc_info=True)
        return False, f"write failed: {e}"
    return True, "ok"


def unset(home_dir, owner_uid, surface, chat_id, key: str, *,
         instance_id: Optional[str] = None) -> Tuple[bool, str]:
    """Remove ONE ``chat.*`` key from the room's overlay — back to its default.

    The counterpart to :func:`set` for a value that cannot round-trip through
    an empty string (``chat.quiet_hours``/``chat.language`` refuse ``""`` at
    ``_coerce``, so ``set(..., "")`` can never clear them). Missing key / file
    is a no-op success, not an error — "unset" already describes the state.
    """
    if not str(key).startswith("chat."):
        return False, f"{key}: not a chat.* key (a room takes chat.* settings only)"
    if key.split(".", 1)[1] not in ChatPolicy.__dataclass_fields__:
        return False, f"unknown chat.* key: {key}"
    path = chat_preferences_path(home_dir, owner_uid, surface, chat_id, instance_id)
    if path is None:
        return False, "empty or unsafe owner id refused (tenant scope)"
    if not path.is_file():
        return True, "ok"  # nothing written yet -> already at default
    try:
        current = _read_flat(path)
    except Exception as e:
        return False, f"{path.name} is unreadable ({e}) — fix or remove it first"
    if key not in current:
        return True, "ok"
    current.pop(key, None)
    try:
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(_render_toml(current), encoding="utf-8")
        os.replace(str(tmp), str(path))
        _CACHE.pop(str(path), None)
    except Exception as e:
        logger.error("chat policy unset failed (%s): %s", path, e, exc_info=True)
        return False, f"write failed: {e}"
    return True, "ok"


def _in_quiet_hours(window: str, now: Optional[float] = None) -> bool:
    """True inside an ``HH-HH`` window (wrapping past midnight).

    A window that cannot be parsed is NOT a reason to go quiet — an unreadable
    value silences a room forever, which is the opposite of what the owner asked
    for when he typed it.
    """
    if not window:
        return False
    try:
        start_s, _, end_s = str(window).partition("-")
        start, end = int(start_s), int(end_s)
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return False
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # wraps past midnight


def mode_allows_trigger(policy: ChatPolicy, *, mentioned: bool, role: str,
                        wake_hit: bool, is_command: bool = False,
                        now: Optional[float] = None) -> bool:
    """May this line wake the agent in this room?

    ADDRESSED means the line reached for the agent: an @mention (or a
    ``text_mention``, or ``/cmd@bot``), a reply to one of the agent's own
    messages — all three arrive as ``mentioned`` from the surface — or a
    ``chat.wake_words`` hit — or (owner/admin only) a slash command, which is
    by definition directed at the bot even with no ``@handle`` attached
    (``is_command`` — the first token of the line starts with ``/``; a plain
    member's ``/…`` line does NOT get this bonus, so ``/groups allow here``
    still can't be forced open by a stranger). This is how ``/mute here 1h``
    reaches the dispatcher's admin-verb routing from a room already demoted to
    ``listen`` — without it, the mute-lift command could never be typed
    without first re-addressing the muted bot.

    The ladder is over WHO gets answered, and being addressed is required at
    every rung but ``active``. **The owner is not exempt**: he is a person in a
    room full of other people, and an agent that answered his every aside would
    both interrupt the room and bill him for it.

    - ``active``  — any line, addressed or not, from anyone.
    - ``mention`` — an ADDRESSED line from anyone, the owner included.
    - ``listen``  — an ADDRESSED line from the owner or a room admin only.
    - ``off``     — nothing, not even the owner (he changes the mode from a
                    seat, not by shouting into the room).

    Belt-and-braces on ``blocked``: the access tier already DENIES that member,
    so reaching here with ``blocked`` means something upstream changed — and the
    answer must still be no.
    """
    if role == "blocked" or policy.mode == "off":
        return False
    mode = policy.mode
    # A mute and a quiet-hours window are the same thing said two ways: the room
    # keeps LISTENING (every line is still logged as context) but only the owner
    # or a room admin gets an answer, and only when they address the agent.
    try:
        muted = float(policy.mute_until or 0) > (now if now is not None else time.time())
    except (TypeError, ValueError):
        muted = False
    if muted or _in_quiet_hours(policy.quiet_hours, now):
        mode = "listen"
    addressed = mentioned or wake_hit or (is_command and role in ("owner", "admin"))
    if mode == "listen":
        return role in ("owner", "admin") and addressed
    if mode == "active":
        return True
    return addressed   # mention — the owner and an admin must be addressed too


def wake_word_hit(policy: ChatPolicy, text: Optional[str]) -> bool:
    """True when one of the room's wake words matches. A bad pattern costs that
    pattern, never the turn (it was already refused at write time; a file edited
    by hand can still carry one)."""
    if not text or not policy.wake_words:
        return False
    for word in policy.wake_words:
        try:
            if re.search(str(word), text, re.IGNORECASE):
                return True
        except re.error as e:
            logger.warning("chat.wake_words: skipping bad pattern %r (%s)", word, e)
    return False


__all__ = ["MODES", "ChatPolicy", "chat_preferences_path", "load", "load_for_chat",
           "mode_allows_trigger", "set", "unset", "wake_word_hit"]
