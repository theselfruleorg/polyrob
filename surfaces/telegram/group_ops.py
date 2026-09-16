"""044 T18/T19: `/groups` and `/mute` — the Telegram seat over
`core.surfaces.group_admin`. Every verb renders the ONE verified text
`group_admin` returns; nothing here touches a store directly.

Role gating (044 proposal §4.7): `allow`/`deny`/`set`/`role`/`service`/`admins`
are owner-only; `mode`/`tail` (and the sibling `/mute` command) are owner OR
that room's own admin. The one carve-out inside `role`: an admin may grant
exactly `blocked` (block a disruptive member) — never `admin`/`member`, which
would let him promote himself or demote another admin.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core.surfaces import group_admin

logger = logging.getLogger(__name__)

_OWNER_ONLY_VERBS = frozenset({"allow", "deny", "list", "set", "role", "service", "admins"})
#: `use` is not an administrative act — it points THIS caller's `here` at a
#: room. Anyone may focus; what they may then DO is re-checked per verb
#: against their role in THAT room, so focusing widens nobody.
_FOCUS_VERBS = frozenset({"use"})
_OWNER_OR_ADMIN_VERBS = frozenset({"mode", "tail"})
_KNOWN_VERBS = _OWNER_ONLY_VERBS | _OWNER_OR_ADMIN_VERBS | _FOCUS_VERBS

_OWNER_ONLY_TEXT = "🔒 Owner only."
_OWNER_OR_ADMIN_TEXT = "🔒 Owner or room admin only."

#: 044 T18 fix round 1 (Important 3): `here` resolves from `identity.source`,
#: which for a DM is the DM itself — silently allowlisting/muting/reconfiguring
#: the OWNER'S OWN DM as if it were a group. Refuse instead of writing bogus
#: state; a DM must name the room explicitly.
_HERE_OUTSIDE_ROOM_TEXT = ('"here" needs a room. From a DM either name it — '
                          '/groups <verb> telegram <chat_id> ... — or point '
                          '"here" at one first: /groups use telegram <chat_id>')
_HERE_OUTSIDE_ROOM_TEXT_MUTE = ('"here" only works inside a group; from a DM name '
                               'the room: /mute telegram <chat_id> <duration>')


def _is_dm(result: Any) -> bool:
    source = result.inbound.identity.source
    return (getattr(source, "chat_type", "dm") or "dm") == "dm"


def _targets_here(result: Any, surface: Optional[str], chat_id: Optional[str]) -> bool:
    """Is the resolved `(surface, chat_id)` the room this message arrived IN?

    044 T18 fix round 2 (Critical 4): `identity.chat_role` is stamped for the
    room the message CAME THROUGH and for no other. A room's admin who names a
    DIFFERENT room explicitly (`/groups mode telegram <other> off`) must be read
    as whatever HE is in THAT room — otherwise one room's admin was an admin of
    every room the agent is in.
    """
    if surface is None and chat_id is None:
        return True          # no explicit target — the verb is about here
    here_surface, here_chat = _here(result)
    return (str(surface or "") == str(here_surface or "")
            and str(chat_id) == str(here_chat if here_chat is not None else ""))


def _resolve_role(result: Any, container: Any = None, surface: Optional[str] = None,
                  chat_id: Optional[str] = None) -> str:
    """The sender's role for THE ROOM THIS VERB TARGETS.

    A group turn already carries `identity.chat_role` (044 T16, stamped at
    the routing boundary) — but only for the room it arrived through, so it is
    trusted ONLY when the verb targets that same room (`_targets_here`). For a
    DM, or for an explicit `<surface> <chat_id>` pair naming another room, the
    role was never resolved for THAT room — consult `group_roles` directly
    (044 T18 fix round 1, Critical 1b) rather than defaulting an admin to
    `member` or, worse, carrying his stamped `admin` across rooms (Critical 4).
    Never raises — an unreadable check reads as `member` (least privilege)."""
    identity = result.inbound.identity
    chat_role = getattr(identity, "chat_role", None)
    here = _targets_here(result, surface, chat_id)
    if chat_role and here:
        return chat_role
    try:
        from surfaces.telegram.harness import _is_admin_owner
        if _is_admin_owner(identity.user_id):
            return "owner"   # the owner is the owner in every room
    except Exception as e:
        logger.debug("group_ops: owner check failed (reading as member): %s", e)
    if surface and chat_id is not None:
        raw_id = getattr(identity, "raw_user_id", None) or identity.user_id
        return group_admin.room_role(container, surface, chat_id, raw_id)
    return "member"


def _here(result: Any) -> Tuple[Optional[str], Optional[str]]:
    source = result.inbound.identity.source
    return getattr(source, "surface_id", None), getattr(source, "chat_id", None)


def _room_title_from_raw(result: Any) -> str:
    """Best-effort chat title from the raw Telegram update, for `/groups allow
    here` with no explicit name. Never raises; an unreadable/absent title is
    just an empty name (the room falls back to its chat id)."""
    raw = getattr(result.inbound, "raw", None)
    try:
        msg = getattr(raw, "message", None)
        if msg is None and isinstance(raw, dict):
            msg = raw.get("message")
        chat = getattr(msg, "chat", None)
        if chat is None and isinstance(msg, dict):
            chat = msg.get("chat")
        title = getattr(chat, "title", None)
        if title is None and isinstance(chat, dict):
            title = chat.get("title")
        return str(title or "").strip()
    except Exception as e:
        logger.debug("group_ops: chat title probe failed: %s", e)
        return ""


def _owner_uid() -> str:
    """The tenant a room's overlay lives under — always the OWNER principal,
    never the (possibly admin) sender: an admin editing `chat.mode` still
    writes into the owner's identity tree, because the owner is who the room
    configuration belongs to.

    The ONE owner-tenant resolver, shared with the CLI writer
    (`cli/commands/owner.py::_group_owner_uid`) and the read side
    (`core/surfaces/chat_policy.py::load_for_chat`)."""
    from core.instance import resolve_owner_user_id
    return resolve_owner_user_id()


def _split_target(rest: List[str], result: Any) -> Tuple[Optional[str], Optional[str], List[str]]:
    """`(surface, chat_id, remaining_args)` from a leading `here` token, or an
    explicit `<surface> <chat_id>` pair (CLI parity: the same grammar the
    `polyrob owner groups` subcommands take)."""
    if rest and rest[0].lower() == "here":
        surface, chat_id = _here(result)
        return surface, (str(chat_id) if chat_id is not None else None), rest[1:]
    if len(rest) >= 2:
        return rest[0], rest[1], rest[2:]
    return None, None, rest


def _focus_home(task_agent: Any) -> str:
    """The data home the focus pointer lives in — the SAME one `group_admin`
    writes room prefs into, so the two can never disagree about which box."""
    return group_admin.data_home(getattr(task_agent, "container", None))


def _focused(task_agent: Any, result: Any) -> Tuple[Optional[str], Optional[str]]:
    """This caller's focused room, or `(None, None)`.

    ⚠️ Keyed on the principal, never on the chat — one admin's `/groups use`
    must not redirect another's `here`.
    """
    from core.surfaces import room_focus
    try:
        found = room_focus.get(_focus_home(task_agent),
                               result.inbound.identity.user_id)
    except Exception as e:
        logger.debug("group_ops: focus read failed (reading as unset): %s", e)
        return None, None
    return found if found else (None, None)


def _resolve_target(task_agent: Any, result: Any, rest: List[str]):
    """`(surface, chat_id, remaining)` — `_split_target`, then the DM focus.

    ⚠️ Being IN a room always wins: `_split_target` resolves `here` from the
    message's own source first, so a focus can never retarget a line typed in
    the room it is about.
    """
    # ⚠️ A DM is NEVER a room. `_split_target`'s `here` reads the message's own
    # source, and a DM HAS a chat_id (the private chat) — so letting it run
    # first would resolve `here` to the DM itself and write room settings into
    # a chat that is not a room. The focus is consulted instead, and its
    # absence is a refusal upstream.
    if rest and rest[0].lower() == "here" and _is_dm(result):
        f_surface, f_chat = _focused(task_agent, result)
        if f_surface and f_chat:
            return f_surface, f_chat, rest[1:]
        return None, None, rest[1:]
    return _split_target(rest, result)


def _gate(verb: str, role: str, rest: List[str]) -> Optional[str]:
    """`None` = allowed; else the refusal text."""
    if verb not in _KNOWN_VERBS:
        return (f"Unknown /groups verb {verb!r}. Try: allow, deny, list, use, mode, "
                f"set, role, tail, service, admins.")
    if verb == "role":
        if role == "owner":
            return None
        # The last token is the role being granted, whether the caller used
        # `here` or an explicit `<surface> <chat_id>` pair.
        granted = rest[-1].lower() if rest else ""
        if role == "admin" and granted == "blocked":
            return None
        return _OWNER_ONLY_TEXT
    if verb in _FOCUS_VERBS:
        return None
    if verb in _OWNER_ONLY_VERBS:
        return None if role == "owner" else _OWNER_ONLY_TEXT
    return None if role in ("owner", "admin") else _OWNER_OR_ADMIN_TEXT


async def groups_reply(task_agent: Any, result: Any, args: List[str]) -> str:
    args = list(args or [])
    verb = args[0].lower() if args else "list"
    rest = args[1:]

    container = getattr(task_agent, "container", None)
    surface = chat_id = None
    tail_args = rest
    if verb != "list":
        surface, chat_id, tail_args = _resolve_target(task_agent, result, rest)
        # `here` in a DM with nothing focused stays a REFUSAL that names both
        # remedies — never a silent pick of "the room that seems likely".
        if rest and rest[0].lower() == "here" and _is_dm(result) and not surface:
            return _HERE_OUTSIDE_ROOM_TEXT

    role = _resolve_role(result, container, surface, chat_id)

    denial = _gate(verb, role, rest)
    if denial is not None:
        return denial

    owner_uid = _owner_uid()

    if verb == "list":
        return group_admin.list_rooms(container, owner_uid)

    if verb == "use":
        from core.surfaces import room_focus
        home, uid = _focus_home(task_agent), result.inbound.identity.user_id
        arg0 = (tail_args[0].lower() if tail_args else
                (rest[0].lower() if rest else ""))
        if arg0 in ("none", "clear", "-"):
            room_focus.clear(home, uid)
            return ('Cleared. "here" now needs a room again — send it inside '
                    'the group, or /groups use telegram <chat_id>.')
        if not surface or chat_id is None:
            cur = _focused(task_agent, result)
            if cur[0]:
                return (f'"here" currently means {cur[0]}:{cur[1]}. '
                        f'/groups use none to clear.')
            return ('Nothing focused. /groups use telegram <chat_id> points '
                    '"here" at a room so you can configure it from this DM. '
                    '/groups list shows the rooms.')
        if not room_focus.set(home, uid, surface, str(chat_id)):
            return "❌ Could not save that (unwritable data home?)."
        return (f'"here" now means {surface}:{chat_id} in this DM. '
                f'Role is still checked per room; /groups use none to clear.')

    if not surface or chat_id is None:
        return f"Usage: /groups {verb} here|<surface> <chat_id> ..."

    if verb == "allow":
        title = " ".join(tail_args).strip() if tail_args else _room_title_from_raw(result)
        return group_admin.allow_here(container, surface, chat_id, title, owner_uid=owner_uid)
    if verb == "deny":
        return group_admin.deny_here(container, surface, chat_id, owner_uid=owner_uid)
    if verb == "mode":
        if not tail_args:
            return "Usage: /groups mode here|<surface> <chat_id> <mention|active|listen|off>"
        return group_admin.set_mode(container, owner_uid, surface, chat_id, tail_args[0])
    if verb == "set":
        if len(tail_args) < 2:
            return "Usage: /groups set here|<surface> <chat_id> <key> <value>"
        key, value = tail_args[0], " ".join(tail_args[1:])
        return group_admin.set_key(container, owner_uid, surface, chat_id, key, value)
    if verb == "role":
        if len(tail_args) < 2:
            return ("Usage: /groups role here|<surface> <chat_id> <user_id> "
                    "<admin|member|blocked>")
        user_ref, new_role = tail_args[0], tail_args[1]
        by = result.inbound.identity.raw_user_id or result.inbound.identity.user_id
        return group_admin.set_role(container, surface, chat_id, user_ref, new_role, by=by)
    if verb == "tail":
        n = 30
        if tail_args and tail_args[0].lstrip("-").isdigit():
            n = int(tail_args[0])
        return group_admin.tail(container, surface, chat_id, n)
    if verb == "service":
        every, max_replies = "30m", 3
        it = iter(tail_args)
        for tok in it:
            low = tok.lower()
            # 044 T20: `off` (alone or after `every`) stops the service job.
            if low in ("off", "stop", "none"):
                every = low
            elif low == "every":
                every = next(it, every)
            elif low == "max":
                spec = next(it, str(max_replies))
                try:
                    max_replies = int(spec)
                except ValueError:
                    return ("Usage: /groups service here|<surface> <chat_id> "
                            f"[every <dur>] [max <n>] — {spec!r} is not a number")
        # 044 T20: the service verb's helper lives in the cron tier (a core
        # module may not import cron) — same ONE-helper contract as group_admin.
        from cron.room_service import service as _room_service
        return _room_service(container, owner_uid, surface, chat_id,
                             every=every, max_replies=max_replies)
    if verb == "admins":
        return await _admins_reply(task_agent, surface, chat_id)

    return f"Unknown /groups verb {verb!r}."


async def _admins_reply(task_agent: Any, surface: str, chat_id: str) -> str:
    """044 T19: Telegram's own admin list, as SUGGESTIONS only — writes
    nothing. Each suggestion renders the exact `/groups role here <id> admin`
    command so the owner confirms with a single copy-paste, never a silent
    auto-grant."""
    container = getattr(task_agent, "container", None)
    harness = container.get_service("telegram_harness") if container is not None else None
    if harness is None or not hasattr(harness, "suggest_admins"):
        return "Not available on this seat."
    suggestions = await harness.suggest_admins(chat_id)
    if not suggestions:
        return f"No (human) Telegram admins found for {surface}:{chat_id}."
    lines = [f"{len(suggestions)} Telegram admin(s) in {surface}:{chat_id} — confirm each:"]
    for s in suggestions:
        lines.append(f"• {s['name']}|{s['id']} — /groups role here {s['id']} admin")
    return "\n".join(lines)




# ---------------------------------------------------------------------------
# 046: the reply-/mute member action
# ---------------------------------------------------------------------------

def _reply_target(result: Any) -> Optional[Tuple[str, str, bool]]:
    """``(raw_user_id, display_name, is_bot)`` of the message this command
    REPLIED to.

    ⚠️ This is what separates the two `/mute` meanings. A reply is a FACT that
    is present or absent, so the split never guesses: a reply targets a MEMBER
    (paid), no reply is the pre-046 room verb (silence the AGENT).

    ⚠️ ``is_bot`` rides along because a member can reply to the AGENT's OWN
    message, and a paid action against the bot would be the room buying the
    agent's silence.
    """
    from surfaces.telegram.harness import _tg_message
    msg = _tg_message(getattr(result.inbound, "raw", None) or {}) or {}
    frm = (msg.get("reply_to_message") or {}).get("from") or {}
    uid = frm.get("id")
    if not uid:
        return None
    name = frm.get("first_name") or frm.get("username") or str(uid)
    return str(uid), str(name), bool(frm.get("is_bot"))


def _member_may_use(container: Any, surface: str, chat_id: str, verb: str) -> bool:
    """Is *verb* in this room's `chat.member_verbs`?

    ⚠️ `chat.member_verbs` sat in CHAT_PREF_SCHEMA with NOTHING reading it until
    046 — a ratchet named it as a dead key. It is the per-room list of verbs a
    plain member may invoke, and it is what admits `/mute` for a member without
    admitting anything else. Default is ``("help",)``, so a room that configured
    nothing refuses.
    """
    from core.surfaces.chat_policy import load_for_chat
    try:
        policy = load_for_chat(group_admin.data_home(container), surface,
                               str(chat_id))
        return verb in (policy.member_verbs or ())
    except Exception as e:
        logger.debug("group_ops: member-verb check failed (%s) — not granted", e)
        return False


def _bot(task_agent: Any):
    """The live aiogram Bot, or None. One resolver for every seat here.

    ⚠️ The first two lookups are the TEST shape and were the only ones here
    until 2026-09-15. Nothing in production sets either: `harness.py` sets
    ``self.bot`` on the HARNESS, and the object that reaches these seats is the
    TaskAgent. So `_bot` was permanently None in prod — `_target_status_fn`
    raised on every call, `_target_protection` read that as PROTECTED (its
    correct fail-closed default), and a plain member buying a ban on another
    plain member got "That person cannot be targeted here". Observed live in
    Playground Env at 17:43:49. `_rights_fn` shares this resolver and would
    have failed one step later for the same reason.

    The container lookups are the production path: `TelegramHarness.start`
    registers the `room_moderator` (with the surface) and the surface itself,
    and both already hold the bot. Resolved LAZILY per call, like
    `RoomModerator._resolve_bot` — the surface outlives a reconnect and the bot
    object does not.

    ⚠️ Returns None rather than a stub when nothing is reachable. A stub would
    make an unreadable member status look like a readable "not an admin", which
    is exactly how a chat administrator would become buyable.
    """
    found = (getattr(task_agent, "bot", None)
             or getattr(getattr(task_agent, "surface", None), "_bot", None))
    if found is not None:
        return found
    container = getattr(task_agent, "container", None)
    if container is None:
        return None
    try:
        mod = container.get_service("room_moderator")
        if mod is not None:
            resolve = getattr(mod, "_resolve_bot", None)
            found = resolve() if callable(resolve) else None
            if found is not None:
                return found
        reg = container.get_service("surface_registry")
        surface = reg.get("telegram") if reg is not None else None
        return (getattr(surface, "_bot", None)
                or getattr(surface, "bot", None)) or None
    except Exception as e:
        logger.debug("group_ops: bot unresolved from the container (%s)", e)
        return None


def _rights_fn(task_agent: Any):
    """A SYNC probe of the bot's rights in a chat, for the mint helper.

    ⚠️ Returns an EMPTY set on any fault, which the mint helper reads as a
    refusal — never sell a moderation action against a permission we merely
    hope we have.
    """
    async def probe(surface: str, chat_id: str) -> set:
        bot = _bot(task_agent)
        if bot is None:
            return set()
        from surfaces.telegram.moderation import bot_rights
        try:
            return set(await bot_rights(bot, chat_id) or ())
        except Exception as e:
            logger.warning("group_ops: rights probe failed (%s) — no rights", e)
            return set()
    return probe


def _target_status_fn(task_agent: Any):
    """A SYNC probe of a TARGET's own status in the chat (046 T5).

    ⚠️ RAISES on any fault, because `_target_protection` reads a raised probe as
    PROTECTED. A live `creator`/`administrator` must never be targetable, and
    our own `group_roles` table only knows the rows we wrote — a chat admin
    granted in Telegram has none.
    """
    async def probe(surface: str, chat_id: str, user_id: str) -> str:
        bot = _bot(task_agent)
        if bot is None:
            raise RuntimeError("no chat connection to read member status with")
        from surfaces.telegram.moderation import member_status
        return await member_status(bot, chat_id, user_id)
    return probe


def _rail_check_fn():
    """Can this instance actually SETTLE a payment in *asset* (046 T8)?

    ⚠️ `offer()` checked only its own flag and the room, so an instance with
    X402_INVOICE_ENABLED off — whose settlement watcher therefore never starts
    (`core/autonomy_runtime.py`) — would mint offers, take real money and settle
    nothing, forever. Returns a REASON string (refuse) or None (ready).
    """
    def check(asset) -> Optional[str]:
        from modules.x402.invoicing import (
            x402_invoicing_enabled, x402_settle_onchain_detect_enabled)
        if not x402_invoicing_enabled():
            return ("invoicing is off on this instance (X402_INVOICE_ENABLED), "
                    "so nothing would ever detect the payment")
        # ⚠️ TWO flags, not one. `X402_INVOICE_ENABLED` starts the watcher;
        # `X402_SETTLE_ONCHAIN_DETECT` is what makes its tick actually SCAN
        # (`settlement_scan._scan_onchain` returns immediately without it). A
        # room action has no payer who can attest and no facilitator lane, so
        # without detection the money arrives and nothing ever notices.
        if not x402_settle_onchain_detect_enabled():
            return ("on-chain payment detection is off on this instance "
                    "(X402_SETTLE_ONCHAIN_DETECT), so a transfer would arrive "
                    "and nothing would notice it")
        chain = (getattr(asset, "chain", "") or "").strip().lower()
        from modules.x402.invoicing import _chain_family
        if _chain_family(chain) == "svm":
            from modules.x402.settlement_scan import solana_settle_enabled
            if not solana_settle_enabled():
                return f"Solana settlement detection is off, so a {chain} payment would not be seen"
            return None
        from modules.x402.settlement_scan import _resolve_scan_target
        if _resolve_scan_target(chain) is None:
            return (f"I cannot watch {chain} for payments, so this room's asset "
                    f"cannot settle here")
        return None
    return check


def _quoter(task_agent: Any):
    """The tools-tier price quoter, for an asset that is not dollar-pegged.

    ⚠️ `offer()` resolves a container service `payment_quoter` that NOTHING ever
    registered, so a room priced in a volatile asset refused every sale with "no
    price available" — the quoter existed and was unreachable. Injected here,
    beside `mint_fn` and `rights_fn`, rather than registered: it is a tools-tier
    object and this is already the seam that carries them across the boundary.

    A prefer-the-container read stays first so a test (or a future registration)
    still wins. Fail-open to None — a missing quote is a REFUSAL upstream, never
    a zero price.
    """
    container = getattr(task_agent, "container", None)
    if container is not None:
        try:
            existing = container.get_service("payment_quoter")
            if existing is not None:
                return existing
        except Exception:
            pass
    try:
        from tools.defi.payment_quote import PaymentQuoter
        return PaymentQuoter()
    except Exception as e:
        logger.warning("group_ops: no payment quoter available (%s)", e)
        return None


def _mint_fn(task_agent: Any):
    """The invoice minter the mint helper calls.

    Injected from HERE rather than imported inside `core`: the layering ratchet
    forbids a `core -> modules` edge, and it reads imports statically, so even a
    call-time import in `room_actions` would be a new upward edge.
    """
    async def mint(*, offer_id, surface, chat_id, verb, target_user_id, asset,
                   amount_raw, price_usd, purpose, ttl_sec=1800):
        from core.instance import resolve_owner_user_id
        from modules.x402.invoicing import create_payment_request
        return await (create_payment_request(
            user_id=resolve_owner_user_id() or "", session_id="",
            amount_usd=float(price_usd), purpose=purpose, chain=asset.chain,
            asset_id=asset.asset_id, amount_raw=int(amount_raw),
            kind="room_action",
            # 046 T7: the INVOICE dies with the OFFER. It used to take the
            # 72-hour default, so a payer could pay a long-expired offer.
            expiry_hours=max(0.1, float(ttl_sec) / 3600.0),
            extra_metadata={"room_action": {
                "offer_id": offer_id, "surface": surface,
                "chat_id": str(chat_id), "verb": verb,
                # ⚠️ The target lives HERE, in metadata, and not in `purpose`:
                # the public challenge endpoint serves the purpose verbatim, so
                # it must never name the person an action was bought against.
                "target": str(target_user_id)}}))
    return mint


def _offer_card(container: Any, offer_id: str, invoice: dict) -> List[dict]:
    """The payment card for an offer, as `OutboundMessage.media` (046 T12).

    ⚠️ Written under the DATA HOME, not a session workspace: a room session is
    PUBLIC and garbage-collected, and the payer needs the picture to outlive it.
    Fail-open — a render fault costs the picture, never the offer.
    """
    try:
        from core.config_policy import room_action_card_enabled
        if not room_action_card_enabled():
            return []
        import os as _os

        from core.surfaces.room_actions import _data_dir
        from modules.pfp.cards import render_invoice_card
        from modules.x402.artifact import build_payment_artifact
        out_dir = _os.path.join(_data_dir(container), "room_actions", "cards")
        _os.makedirs(out_dir, exist_ok=True)
        out_path = _os.path.join(out_dir, f"{offer_id}.png")
        artifact = build_payment_artifact(invoice)
        rendered = render_invoice_card(invoice, artifact, out_path)
        return [{"kind": "image", "path": str(rendered), "caption": None}]
    except Exception as e:
        logger.warning("group_ops: offer card render failed (%s) — text only", e)
        return []



async def _awaited(value):
    """Await *value* if awaitable, else pass it through.

    The real seams here are async; a test double patched over `offer`,
    `_apply_free` or `_perform` is usually a plain function. Tolerating both
    keeps the doubles readable without pretending production is synchronous.
    """
    import inspect
    if inspect.isawaitable(value):
        return await value
    return value

async def _perform(task_agent: Any, offer_row, eff, until_ts: int):
    """Apply one effect through the ONE adapter.

    ⚠️ Awaited, never bridged. This ran through `run_coroutine_sync`, which put
    the aiogram call on a background loop while the bot's session lived on the
    caller's — so the owner's FREE `/ban` raised "got Future attached to a
    different loop" and did nothing (Playground Env, 2026-09-15 17:55:10).

    ⚠️ The free path used to call `restrict_member` directly and render "muted"
    whatever verb it was given, so adding `/ban` to it would silently have
    muted. There is one adapter now and both paths use it.
    """
    from surfaces.telegram.room_moderator import RoomModerator
    moderator = None
    container = getattr(task_agent, "container", None)
    if container is not None:
        try:
            moderator = container.get_service("room_moderator")
        except Exception:
            moderator = None
    if moderator is None:
        moderator = RoomModerator(getattr(task_agent, "surface", None),
                                  bot=_bot(task_agent))
    return await moderator(offer_row, eff, until_ts)


async def _apply_free(task_agent: Any, surface: str, chat_id: str, verb: str,
                      target_id: str, target_name: str, duration: str):
    """An owner or room admin acts FREE.

    ⚠️ They already hold the authority; charging them for it is theatre. The
    protections still apply — an admin cannot mute the owner or another admin —
    so this goes through the same catalog, the same checks and the same
    adapter, just with no invoice.
    """
    import time as _t

    from core.surfaces import room_actions
    from core.surfaces.command_reply import CommandReply

    eff = room_actions.effect(verb)
    if eff is None:
        return f"I do not know how to {verb}."
    seconds = room_actions.parse_duration(duration) if eff.needs_duration else 0
    if eff.needs_duration and not seconds:
        return f"{duration!r} is not a duration (use e.g. 30m, 2h, 1d)."
    container = getattr(task_agent, "container", None)
    identity_requester = ""   # the free path has no payer
    protected, why = await room_actions._target_protection(
        container, surface, str(chat_id), str(target_id), identity_requester,
        eff=eff, target_status_fn=_target_status_fn(task_agent))
    if protected:
        return f"❌ {why[0]}"
    if _bot(task_agent) is None:
        return "❌ I have no chat connection to do that with."
    held = _rights_fn(task_agent)(surface, str(chat_id))
    if eff.telegram_right not in held:
        return (f"❌ I need the {eff.telegram_right} permission in this chat "
                f"to {verb} anyone.")
    row = _FreeRow(surface=surface, chat_id=str(chat_id), verb=eff.verb,
                   target_user_id=str(target_id), target_name=target_name,
                   duration_sec=seconds)
    res = await _awaited(
        _perform(task_agent, row, eff, int(_t.time() + (seconds or 3600))))
    if not getattr(res, "ok", False):
        return f"❌ Telegram refused: {getattr(res, 'reason', '')}"
    return CommandReply(room_actions.receipt(row, eff), to_room=True)


@dataclass(frozen=True)
class _FreeRow:
    """The offer shape the adapter and the receipt renderer read, for the free
    path that has no offer row."""
    surface: str
    chat_id: str
    verb: str
    target_user_id: str
    target_name: str
    duration_sec: int



#: The word that makes an owner or admin buy an action instead of taking it.
_PAY_FLAG = "pay"


def split_pay_flag(args):
    """``(remaining_args, wants_to_pay)``.

    ⚠️ Free-by-default is correct — someone who already holds the authority
    should not be taxed for using it. But it also means the one person most
    likely to DEMONSTRATE the paid rail is the one person who can never trigger
    it. One word opts out.

    ⚠️ The token is REMOVED from the args. Left in place it reaches
    `parse_duration` and refuses the verb. Whole-token match only, so `payment`
    or `paypal` never trips it.
    """
    rest, wants_pay = [], False
    for a in list(args or []):
        if str(a).strip().lower() == _PAY_FLAG:
            wants_pay = True
            continue
        rest.append(a)
    return rest, wants_pay

async def _moderation_reply(task_agent: Any, result: Any, args: List[str], verb: str):
    """`/mute`, `/unmute`, `/ban`, `/unban` — ONE body, four verbs.

    ⚠️ Each of these means TWO things separated by a FACT, never a guess. In
    REPLY to a message it targets that MEMBER (046: free for the owner and room
    admins, paid for a member). With no reply, `/mute` is the pre-046 room verb
    that silences the AGENT and the other three have no room meaning.
    """
    container = getattr(task_agent, "container", None)
    target = _reply_target(result)

    if target is None:
        if verb != "mute":
            return (f"/{verb} names a person — reply to their message with "
                    f"/{verb}" + (" <duration>" if verb in ("mute", "ban") else "")
                    + ".")
        # --- the pre-046 room verb, byte-identical ---
        args = list(args or [])
        if args and args[0].lower() == "here" and _is_dm(result):
            return _HERE_OUTSIDE_ROOM_TEXT_MUTE
        surface, chat_id, rest = _split_target(args, result)
        role = _resolve_role(result, container, surface, chat_id)
        if role not in ("owner", "admin"):
            return _OWNER_OR_ADMIN_TEXT
        if not surface or chat_id is None or not rest:
            return "Usage: /mute here|<surface> <chat_id> <duration> (e.g. 2h)"
        return group_admin.mute(container, _owner_uid(), surface, chat_id, rest[0])

    # --- 046: a reply names a PERSON ---
    if _is_dm(result):
        return (f"/{verb} in reply only works inside a group — from a DM name "
                f"the room: /mute telegram <chat_id> <duration>")
    surface, chat_id = _here(result)
    args, wants_pay = split_pay_flag(args)
    duration = (list(args) or ["1h"])[0]
    target_id, target_name, target_is_bot = target
    if target_is_bot:
        # Replying to the AGENT's own message names the bot. It is never a
        # target — and a paid one would be the room buying the agent's silence.
        return "❌ That person cannot be targeted here."
    role = _resolve_role(result, container, surface, chat_id)
    identity = result.inbound.identity
    requester = str(getattr(identity, "raw_user_id", None) or identity.user_id)

    if role in ("owner", "admin") and not wants_pay:
        return await _awaited(_apply_free(
            task_agent, surface, str(chat_id), verb, target_id,
            target_name, duration))
    # ⚠️ Paying buys the PAYMENT FLOW, never authority: `offer()` re-runs every
    # target protection, so an admin who pays still cannot touch the owner,
    # another admin, or himself.

    # ⚠️ `chat.member_verbs` is a MEMBER gate, not an authority gate. An owner
    # or admin choosing to pay already holds the authority — refusing him for a
    # grant he does not need would make the demo impossible in exactly the rooms
    # that never granted the verb to members.
    if (role not in ("owner", "admin")
            and not _member_may_use(container, surface, str(chat_id), verb)):
        return (f"/{verb} is not a member verb in this room. An owner enables "
                f"it with `/groups set here member_verbs help,{verb}`.")

    return await _paid_offer(task_agent, container, surface, str(chat_id), verb,
                             target_id, target_name, requester, duration)


#: Refusals a plain MEMBER is told IN THE ROOM.
#:
#: ⚠️ 2026-09-15, Playground Env: a member replied to his own message with
#: `/ban@tmachinroBot 1h`, the self-target guard fired correctly, and the
#: refusal went to his DM as a bare string — so the room saw NOTHING and three
#: people concluded the feature was broken. The offer and the receipt already
#: go to the room; a refusal that names only the member belongs there too.
#:
#: ⚠️ Everything NOT in this set stays owner-only, deliberately. A refusal that
#: describes the PAYMENT RAIL's condition — it cannot settle this asset, the
#: mint failed, the owner is unresolvable, the bot lacks a Telegram right —
#: tells a room exactly when the rail is weak, which is information for
#: somebody probing it. Unclassified codes fail CLOSED (owner-only).
ROOM_FACING_REFUSALS = frozenset({
    "self_target",       # "you cannot buy this against yourself"
    "protected_target",  # already the deliberately-vague public wording
    "payer_cap",         # you have bought enough today
    "target_cap",        # that person has been hit enough today
    "disabled",          # this room is not selling right now
    "paused",            # the owner stopped sales; silence would confuse
    "unknown_verb",
    "bad_duration",
})


def refusal_reply(res: Any):
    """A refused offer, routed by whether it names the MEMBER or the RAIL."""
    from core.surfaces.command_reply import CommandReply
    if getattr(res, "reason_code", "") in ROOM_FACING_REFUSALS:
        return CommandReply(res.text, to_room=True)
    return res.text


async def _paid_offer(task_agent: Any, container: Any, surface: str, chat_id: str,
                verb: str, target_id: str, target_name: str, requester: str,
                duration: str):
    """Mint the offer, or spend a credit, and put the answer WHERE THE PAYER IS."""
    from core.surfaces import room_actions
    from core.surfaces.command_reply import CommandReply

    res = await _awaited(room_actions.offer(
        container, surface=surface, chat_id=chat_id, verb=verb,
        target_user_id=target_id, target_name=target_name,
        requester_id=requester, duration=duration,
        rights_fn=_rights_fn(task_agent), mint_fn=_mint_fn(task_agent),
        quoter=_quoter(task_agent), rail_check=_rail_check_fn(),
        target_status_fn=_target_status_fn(task_agent)))
    if not res.ok:
        # A refusal that names only the MEMBER is answered in the room; one that
        # names the RAIL's condition stays owner-only. See ROOM_FACING_REFUSALS.
        return refusal_reply(res)
    if res.reason_code == "credit_redeemed":
        # Already paid for, once. Apply it now and render the receipt.
        applied = await _awaited(room_actions.apply(container, res.offer_id))
        return CommandReply(f"{res.text}\n{applied.text}", to_room=True)
    media = _offer_card(container, res.offer_id, res.invoice or {})
    return CommandReply(res.text, to_room=True, media=tuple(media))


async def mute_reply(task_agent: Any, result: Any, args: List[str]):
    return await _moderation_reply(task_agent, result, args, "mute")


async def unmute_reply(task_agent: Any, result: Any, args: List[str]):
    return await _moderation_reply(task_agent, result, args, "unmute")


async def ban_reply(task_agent: Any, result: Any, args: List[str]):
    return await _moderation_reply(task_agent, result, args, "ban")


async def unban_reply(task_agent: Any, result: Any, args: List[str]):
    return await _moderation_reply(task_agent, result, args, "unban")


__all__ = ["ROOM_FACING_REFUSALS", "ban_reply", "groups_reply",
           "split_pay_flag",
           "mute_reply", "paid_reply", "refusal_reply",
           "unban_reply", "unmute_reply"]


_PAID_OWNER_ONLY_VERBS = frozenset({"enable", "disable", "price", "asset"})
_PAID_OWNER_OR_ADMIN_VERBS = frozenset({"status", "offers", "cancel"})
#: 046: the only `/paid` verb a plain MEMBER may reach — and only when the room
#: granted `paid` in `chat.member_verbs`. Read-only: it renders THIS room's price
#: list and nothing else. `status` is deliberately NOT here: it names the caps,
#: the asset, the open offers and the owner's configuration remedies.
_PAID_MEMBER_VERBS = frozenset({"prices"})
_PAID_VERBS = (_PAID_OWNER_ONLY_VERBS | _PAID_OWNER_OR_ADMIN_VERBS
               | _PAID_MEMBER_VERBS)


async def paid_reply(task_agent: Any, result: Any, args: List[str]):
    """`/paid` — configure and inspect THIS room's paid actions (046).

    Role split mirrors `/groups`: configuring what the room SELLS (enable,
    disable, price, asset) is the owner's; reading and withdrawing an offer is
    reachable by that room's own admin too. Every verb renders the ONE text
    `core.surfaces.room_action_admin` returns; nothing here touches a store.

    046: a plain MEMBER reaches exactly ONE thing — this room's PRICE LIST,
    rendered by `room_actions.render_member_prices` (the one price reader) and
    delivered IN THE ROOM. He needs the room's own `chat.member_verbs` grant to
    get here at all, and no grant can widen him past that read.
    """
    from core.surfaces import room_action_admin as adm
    from core.surfaces.room_actions import render_member_prices

    container = getattr(task_agent, "container", None)
    args = list(args or [])
    verb = (args[0].lower() if args else "")

    # ⚠️ The room check now runs BEFORE the unknown-verb echo (it used to run
    # after), because the role decides which vocabulary the echo may name: a
    # member must never be handed the owner's `/paid` verb list. An owner's
    # `/paid <typo>` in a DM therefore gets "run it inside the group" — also
    # true, and the more useful of the two sentences.
    surface, chat_id = _here(result)
    if _is_dm(result):
        # 2026-09-15, owner ergonomics: `/paid` used to refuse every DM, so a
        # room's prices, caps and member verbs could only be typed in front of
        # its members. It now administers the FOCUSED room — and only that;
        # `_resolve_role` below still resolves the caller's role for THAT room
        # from the roles store, so reaching a room here grants nothing.
        surface, chat_id = _focused(task_agent, result)
        if not surface or chat_id is None:
            return ("/paid needs a room. Point this DM at one — /groups use "
                    "telegram <chat_id> — or run it inside the group. "
                    "/groups list shows the rooms.")
    if not surface or chat_id is None:
        return ("/paid configures the room you are in — run it inside the "
                "group.")

    role = _resolve_role(result, container, surface, str(chat_id))
    if role not in ("owner", "admin"):
        # 046: a MEMBER's `/paid` is the PRICE LIST, delivered IN THE ROOM. Bare
        # `/paid` means prices for him — `status` (the owner default) names the
        # caps, the asset, the open offers and the configuration remedies, none
        # of which is his. Routing already required this room's own
        # `chat.member_verbs` grant, so reaching here at all is an owner
        # decision; what he reaches is still only this one read.
        from core.surfaces.command_reply import CommandReply
        if verb and verb not in _PAID_MEMBER_VERBS:
            return CommandReply(
                f"In this room `/paid` shows the price list — `{verb}` is the "
                f"owner's. Send `/paid` on its own.", to_room=True)
        return CommandReply(
            render_member_prices(container, surface=surface,
                                 chat_id=str(chat_id)), to_room=True)

    verb = verb or "status"
    if verb not in _PAID_VERBS:
        return (f"Unknown /paid verb {verb!r}. Try: "
                f"{', '.join(sorted(_PAID_VERBS))}.")
    if verb in _PAID_OWNER_ONLY_VERBS and role != "owner":
        return _OWNER_ONLY_TEXT
    if verb in _PAID_MEMBER_VERBS:
        # An owner/admin typing the member verb gets the MEMBER's own view, so
        # he can read exactly what this room advertises rather than infer it
        # from the configuration.
        return render_member_prices(container, surface=surface,
                                    chat_id=str(chat_id))

    rest = args[1:]
    if verb == "status":
        return adm.status(container, surface, str(chat_id))
    if verb == "enable":
        return adm.enable(container, surface, str(chat_id))
    if verb == "disable":
        return adm.disable(container, surface, str(chat_id))
    if verb == "offers":
        return adm.offers(container, surface, str(chat_id))
    if verb == "asset":
        if not rest:
            return "Usage: /paid asset <asset_id> (see polyrob wallet asset list)"
        return adm.set_asset(container, surface, str(chat_id), rest[0])
    if verb == "price":
        if len(rest) < 2:
            return "Usage: /paid price <verb> <usd> (e.g. /paid price mute 0.50)"
        try:
            usd = float(rest[1])
        except ValueError:
            return f"❌ {rest[1]!r} is not a price."
        return adm.set_price(container, surface, str(chat_id), rest[0], usd)
    if verb == "cancel":
        if not rest:
            return "Usage: /paid cancel <offer_id>"
        identity = result.inbound.identity
        by = str(getattr(identity, "raw_user_id", None) or identity.user_id)
        return adm.cancel(container, rest[0], by=by)
    return f"Unknown /paid verb {verb!r}."
