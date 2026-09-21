"""P3: channel inbound dispatcher — route_inbound (pure decision table).

One inbound message from any surface resolves to exactly one action:

  COMMAND      text is a control verb (/task /cancel /new /help) — wins over an
               active session, so /cancel cancels instead of being steered in.
  STEER        a bound session exists for this chat-scoped key (warm) — inject the
               message into the running session (the caller rehydrates if the
               orchestrator is gone; a warm chat NEVER diverts).
  CHAT_FASTPATH cold + CHAT_INTENT_CLASSIFIER ON + an injected is_chitchat predicate
               says it's chitchat — the optional ChatAgent cost-optimization.
  TASK_AGENT   default — the unified Task agent IS the front door (Option A).

cold/warm is decided by SessionChatRegistry row existence (single SSOT, durable,
cross-worker safe). The dispatcher never calls an LLM: an intent classifier, if any,
is injected as is_chitchat (sync or async). Fully fail-open: any lookup/predicate
error degrades to TASK_AGENT, never raises into the inbound handler.
"""
import inspect
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from core.surfaces.access import (
    FORGEABLE_NETWORK_SURFACES as _ACCESS_FORGEABLE,
)
from core.surfaces.access_log import record_route
from core.surfaces.envelopes import InboundMessage
from core.surfaces.session_chat_registry import build_session_key

logger = logging.getLogger(__name__)

# Owner-admin verbs (§7.1/§7.2b, owner-UX P4 T2): the phone-only headless
# owner's control surface — the approve loop, open asks, the messaging
# allowlist, pause/resume control, scheduled and goal work, money movement,
# and access management, plus read-only status. The surface handler
# owner-gates them by principal; routing here only classifies them as COMMAND
# so they win over an active session.
# A verb absent from this tuple is NOT a command: it falls through to STEER /
# TASK_AGENT and reaches the agent as chat text. Every surface handler verb must
# therefore appear here — pinned by
# tests/unit/surfaces/telegram/test_owner_control_plane.py::
# test_every_owner_verb_is_routable.
_COMMANDS = ("/task", "/cancel", "/new", "/help",
             "/inbox",   # 043 D1: the one list of what needs the owner; /pending stays
             "/book",    # 043 D1: the ledger against every money chain
             "/pending", "/approve", "/reject", "/asks", "/fulfill",
             "/allow", "/deny", "/allowlist",
             "/groups", "/mute",  # 044 T18: room presence admin (owner or that room's admin)
             "/paid",             # 046: paid room actions (owner or room admin)
             "/ban", "/unban", "/unmute",   # 046 phase 2: the rest of the catalog
             "/halt", "/resume", "/pause",  # owner pause record (031; /halt = alias of /pause)
             "/cron", "/goal", "/wallet", "/invoices", "/settle",  # G13 write verbs
             "/trade",  # owner-launched money-granted run (2026-09-09)
             "/bridge",  # 037 cross-chain move; owner-only, approval-gated
             "/launch", "/deploy", "/lp",  # token/liquidity writes; owner-only, capped
             "/claim",   # 046/E6: the launchpad creator-fee claim, from the phone
             "/nft",      # E7: hold / look / send / revoke, owner seat
             "/dapp",     # E9: the wallet sessions a page holds, and cutting one off
             "/identity",  # E8: this instance's own ERC-8004 registration
             "/contacts",  # E10: who replied, and the transcript with one of them
             "/status", "/avatar", "/mode", "/recap", "/journey", "/goals", "/prefs", "/config",
             "/cwd",   # C67: where I am working right now (read-only)
             "/missed",
             "/apps",  # 032 durable app service (approve an address, health, kill, logs)
             "/mcp",   # per-tenant MCP servers; owner-only (core/mcp_admin.py)
             "/kb", "/files",
             "/dev",  # owner ↔ on-host dev-loop rail (proposal 027 WS-1)
             "/start")  # Telegram first-contact convention -> welcome (030 L9);
                        # NOT owner-gated, so it stays out of _OWNER_ADMIN_COMMANDS

# 030 L9: a leading token that LOOKS like a command (Telegram command grammar,
# optional @botname suffix) but isn't in _COMMANDS must still classify as COMMAND —
# the surface answers with help + a suggestion in one cheap reply. Falling through
# to STEER/TASK_AGENT burned a full LLM turn on every typo, and the harness's
# "unknown command -> help" branch was unreachable from a real inbound.
_COMMAND_SHAPE_RE = re.compile(r"^/[a-z][a-z0-9_]{0,31}$")

# 044 T18 fix round 1 (Critical 1a): the verbs a ROOM ADMIN (not just the
# owner) may reach as a COMMAND from inside the room he administers. Without
# this the GROUP_MEMBER branch below never inspects the text at all, so
# `/groups mode here listen` from an admin fell straight through to
# GROUP_TURN/TASK_AGENT — the admin half of "owner OR room admin" was
# unreachable through any real Telegram path. A plain MEMBER's slash line
# still stays a room turn (member verbs are a later, separate schema row).
_GROUP_ADMIN_COMMANDS = frozenset({"/groups", "/mute", "/unmute", "/ban",
                                   "/unban", "/paid", "/cancel", "/new",
                                   "/help"})

#: 046: verbs a room's OWNER may grant to plain members via `chat.member_verbs`.
#:
#: ⚠️ A CLOSED set, and deliberately not "whatever the owner typed". A room key
#: is owner-written but the SET of grantable verbs is code, so a typo — or a
#: later verb nobody thought about as member-reachable — can never become a
#: member capability by configuration alone. The room must ALSO list it.
#: ⚠️ `/paid` is member-grantable as a READ ONLY: the handler gives a plain
#: member exactly one verb (this room's price list) and refuses every
#: configuration verb whatever the grant says. Without it the price list was
#: reachable only by the people who set it.
_MEMBER_GRANTABLE_COMMANDS = frozenset({"/mute", "/unmute", "/ban", "/unban",
                                        "/help", "/paid"})


def _member_verb_granted(container, surface, chat_id, token: str) -> bool:
    """Has this room granted *token* to plain members (`chat.member_verbs`)?

    Fail-CLOSED: an unreadable policy grants nothing. The default is
    ``("help",)``, so a room that configured nothing admits only `/help` —
    a member's `/mute` there stays ordinary room chatter, exactly as before 046.
    """
    verb = (token or "").lstrip("/").strip().lower()
    if not verb:
        return False
    try:
        from core.runtime_paths import data_dir_or_home
        from core.surfaces.chat_policy import load_for_chat
        cfg = getattr(container, "config", None) if container is not None else None
        policy = load_for_chat(data_dir_or_home(getattr(cfg, "data_dir", None)),
                               str(surface), str(chat_id))
        return verb in (policy.member_verbs or ())
    except Exception as e:
        logger.debug("member verb check failed for %s:%s (%s) — not granted",
                     surface, chat_id, e)
        return False

ChitchatPredicate = Callable[[InboundMessage], Union[bool, Awaitable[bool]]]


# P1-6: forgeable-sender network surfaces whose senders can NEVER be the bound owner
# in v1 (the From:/address is trivially spoofable). Such a surface must never fall
# through to the legacy obey-path when the correspondent tier model is off — it is
# correspondent-or-denied by construction.
#
# D1: the SSOT is `core.surfaces.access.FORGEABLE_NETWORK_SURFACES`, imported
# here rather than re-declared. The two copies guarded different halves of one
# rule — this one the model-OFF path, while the model-ON path's pairing branch
# had no surface filter at all, so a paired email address became OWNER.
_FORGEABLE_NETWORK_SURFACES = _ACCESS_FORGEABLE


class RouteKind(str, Enum):
    COMMAND = "command"
    STEER = "steer"
    CHAT_FASTPATH = "chat_fastpath"
    TASK_AGENT = "task_agent"
    DENIED = "denied"  # polyrob D3: ingress blocked (pairing required / not paired)
    CORRESPONDENT_DATA = "correspondent_data"  # WS-A: third-party reply -> DATA into
                                               # the originating session (never a command)
    GROUP_TURN = "group_turn"                  # 044 T14: a line in an allowed ROOM runs
                                               # the bound PUBLIC session with a
                                               # <group-context> block + an attributed
                                               # <addressed> line (member = data,
                                               # owner/admin = steer)


@dataclass
class RouteDecision:
    kind: RouteKind
    session_key: str
    session_id: Optional[str] = None
    command: Optional[str] = None
    pairing_code: Optional[str] = None  # set on DENIED so the surface can tell the
                                        # user how to get approved (None = anon/no code)
    silent: bool = False                # W3: DENIED without a user-facing reply
                                        # (group denials never spam channels)
    #: 045 I2 — WHY, in one short stable slug (see DENIAL_REASONS). Explanation
    #: only: it never changes an OUTCOME. Without it `record_route`'s
    #: `getattr(decision, "reason", None)` was always None and the rollup's
    #: `v IS NOT NULL` filter dropped every row, so `top_denial_reasons` was
    #: STRUCTURALLY always empty and twelve distinct deny branches read to the
    #: owner as one indistinguishable "denied".
    reason: Optional[str] = None
    #: The resolved access tier, when the path that decided knows one. The
    #: `tier` parameter of `record_route` existed, the attrs allowlist carried
    #: it and every unit test passed it — the one PRODUCTION call site never
    #: did, so the column was always NULL in prod.
    tier: Optional[str] = None


#: The denial VOCABULARY the owner reads. Short, lowercase, stable — a rename
#: silently reshapes a brief the owner has learned to scan. Pinned per branch by
#: tests/unit/core/surfaces/test_route_denial_reasons.py.
DENIAL_REASONS = (
    "pairing_required",        # POLYROB_REQUIRE_PAIRING: unpaired non-owner
    "group_disabled",          # non-DM while GROUP_CHAT_ENABLED is off
    "rooms_need_singular_chat",  # GROUP_CHAT_ENABLED on, the surface bus absent
    "tier_denied",             # resolve_access_tier said DENIED
    "bot_loop_guard",          # another bot's line, unmentioned
    "room_cap",                # per-chat reply cap / per-member cooldown
    "no_mention",              # group mention gate on, bot not mentioned
    "group_fault",             # group model raised -> fail CLOSED
    "no_origin_session",       # correspondent with nothing to attach the reply to
    "tier_fault",              # tier model raised -> fail CLOSED
    "forgeable_sender",        # email with the correspondent model off
)


async def route_inbound(
    container: Any,
    inbound: InboundMessage,
    *,
    is_chitchat: Optional[ChitchatPredicate] = None,
) -> RouteDecision:
    """Route one inbound, then record the decision (045 lane 1).

    The recording lives HERE rather than at each of the impl's return sites: the
    decision table has 17 of them and a future branch would silently skip the
    ledger. One wrapper covers every present and future path by construction.

    The recorder is fail-open (``access_log.record_route`` swallows), and this
    try/except is the second belt: a monkeypatched or broken recorder must never
    change what the dispatcher decided.

    ``trace`` carries the resolved access tier OUT of the impl (045 I2). A tier
    that is resolved and then falls through to the legacy COMMAND/STEER/
    TASK_AGENT flow is no longer in scope at those return sites, and the one
    production call site therefore recorded ``tier=NULL`` for every allowed
    route. One out-parameter beats stamping five more returns.
    """
    trace: dict = {}
    decision = await _route_inbound_impl(container, inbound,
                                         is_chitchat=is_chitchat, _trace=trace)
    if decision.tier is None and trace.get("tier"):
        decision.tier = trace["tier"]
    try:
        record_route(inbound, decision)
    except Exception:
        logger.debug("route_inbound: access-log record skipped", exc_info=True)
    return decision


#: Surfaces already told (once) that their rooms need the Singular Chat bus.
#: One WARN per surface per process: this fires on EVERY room line, and a
#: misconfiguration the operator can only fix once must not become a log flood.
_ROOMS_NEED_BUS_WARNED: set = set()


def _warn_rooms_need_bus(surface_id: str) -> None:
    """044 C2: name the missing flag, once per surface. Never raises."""
    try:
        if surface_id in _ROOMS_NEED_BUS_WARNED:
            return
        _ROOMS_NEED_BUS_WARNED.add(surface_id)
        logger.warning(
            "%s room messages are being DENIED: GROUP_CHAT_ENABLED is on but the "
            "Singular Chat bus is not installed — rooms need SINGULAR_CHAT_ENABLED "
            "(the chat<->session registry, room caps, ledger and outbound router "
            "all come from it). Set SINGULAR_CHAT_ENABLED=true and restart.",
            surface_id)
    except Exception:  # pragma: no cover - a logging fault must not deny differently
        pass


def _is_groups_verb(text: str) -> bool:
    """Is this line the `/groups` command? (Telegram's `@botname` suffix included.)"""
    first = (text or "").strip().split(" ", 1)[0].lower().split("@", 1)[0]
    return first == "/groups"


def _owner_principal_of(user_id: Any) -> bool:
    """The bound owner principal, room rules (no pairing, no local bypass).
    Fail-CLOSED: an unreadable owner check is not the owner."""
    try:
        from core.surfaces.access import is_room_owner
        return is_room_owner(user_id)
    except Exception as e:
        logger.debug("route_inbound: room owner pre-check failed (not owner): %s", e)
        return False


def _room_session_is_stale(row: Optional[dict], session_key: str) -> bool:
    """044 I11: has the bound ROOM session crossed the session boundary?

    The SAME `should_start_fresh` policy the STEER site applies — a room is a
    chat like any other, and its session must age out. The member branch
    returned GROUP_TURN upstream of that check, so a room accumulated ONE session
    forever: a growing history, yesterday's conversation folded into today's
    answer, and the odd asymmetry that the owner's line in the same room DID
    reset (it falls through to STEER) while a member's never did.

    Fail-OPEN to "not stale": a policy error must keep the room on its existing
    session, never spuriously wipe a live conversation.
    """
    if not row:
        return False
    try:
        import time as _time
        from core.surfaces.config import SurfaceConfig
        from core.surfaces.session_policy import should_start_fresh
        fresh, reason = should_start_fresh(
            row, now=_time.time(),
            idle_minutes=SurfaceConfig.session_idle_minutes(),
            daily_hour=SurfaceConfig.session_reset_hour(),
            mode=SurfaceConfig.session_reset_mode(),
        )
        if fresh:
            logger.info("route_inbound: room session reset (%s) for %s",
                        reason, session_key)
        return bool(fresh)
    except Exception as e:
        logger.debug("route_inbound room boundary policy skipped: %s", e)
        return False


def _note_tier(trace: Optional[dict], tier: Any) -> None:
    """Remember a resolved tier for the recorder. Never raises — an unhashable
    or odd tier costs a column, never a routing decision."""
    try:
        if trace is not None:
            trace["tier"] = getattr(tier, "value", None) or str(tier)
    except Exception:
        pass


async def _route_inbound_impl(
    container: Any,
    inbound: InboundMessage,
    *,
    is_chitchat: Optional[ChitchatPredicate] = None,
    _trace: Optional[dict] = None,
) -> RouteDecision:
    user_id = inbound.identity.user_id
    session_key = build_session_key(inbound.identity.source, user_id)
    text = (inbound.text or "").strip()

    # 0-revive) T1.5: ANY inbound message revives a previously dead-marked target —
    # receiving proves the sender is reachable again. Clears both identity tuples
    # (outbound dest for telegram is the CHAT id; inbound sender is the USER id —
    # they coincide for DMs, differ for groups). Idempotent no-op if never marked.
    # Fully fail-open: any lookup/store fault must never block routing, and this
    # runs before every other gate below so a revive never depends on tier/pairing
    # outcomes.
    try:
        from core.config_policy import dead_target_registry_enabled
        if dead_target_registry_enabled():
            dead_targets = container.get_service("dead_targets") if container else None
            if dead_targets is not None:
                _surface_id = getattr(inbound.identity.source, "surface_id", None)
                _chat_id = getattr(inbound.identity.source, "chat_id", None)
                _sender_id = inbound.identity.raw_user_id or user_id
                if _surface_id:
                    dead_targets.clear(_surface_id, _chat_id or "")
                    dead_targets.clear(_surface_id, _sender_id or "")
    except Exception as e:  # never block routing on a revive fault
        logger.debug("route_inbound dead-target revive skipped: %s", e)

    # 0) ACCESS GATE (polyrob D3) — when POLYROB_REQUIRE_PAIRING is on, an unpaired
    #    non-owner is denied (and issued a pairing code). Fail-open + default-off, so
    #    this is byte-identical until an operator opts into pairing.
    try:
        from core.pairing import guard_inbound
        surface_id = getattr(inbound.identity.source, "surface_id", None)
        denial = guard_inbound(container, user_id, surface_id=surface_id)
        if denial is not None:
            return RouteDecision(RouteKind.DENIED, session_key,
                                 pairing_code=denial.pairing_code,
                                 reason="pairing_required")
    except Exception as e:  # never block routing on a guard fault
        logger.debug("route_inbound access-gate skipped: %s", e)

    # 0a-groups) W3 GROUP CHAT (opt-in GROUP_CHAT_ENABLED, default OFF). In an
    #    allowlisted group chat: the owner gets the legacy flow (mention-gated);
    #    a member's message is DATA into the bound group session
    #    (mention-gated, correspondent rail = untrusted-wrap + capability
    #    taint); everything else is a SILENT deny (no pairing spam into
    #    channels). Fail-CLOSED once the flag is on.
    #    Flag OFF: group/channel messages are silently DENIED here — the old
    #    fall-through meant "obey everyone in the room" on surfaces with no
    #    sender allowlist of their own (discord/slack/signal have none; only
    #    telegram has ALLOWED_TELEGRAM_USER_IDS). DMs are untouched.
    _chat_type = getattr(inbound.identity.source, "chat_type", "dm") or "dm"
    _group_enabled = False
    if _chat_type != "dm":
        try:
            from core.surfaces.config import SurfaceConfig as _SC
            _group_enabled = _SC.group_chat_enabled()
        except Exception as e:
            logger.debug("route_inbound group flag read failed (treat as off): %s", e)
            _group_enabled = False
        if not _group_enabled:
            logger.info(
                "route_inbound: %s %s message denied — GROUP_CHAT_ENABLED off",
                getattr(inbound.identity.source, "surface_id", "?"), _chat_type,
            )
            return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                 reason="group_disabled")
    #: 044 T14: set when an OWNER line in an allowed room passed every group gate.
    #: Consumed at the STEER site below (a warm room session answers as a room
    #: TURN, not a bare steer) — never before COMMAND.
    _room_owner_turn = False
    #: 044 I1: the room's hourly reply budget is already spent. Resolved inside
    #: the group block, refused at each TURN return site (never before COMMAND —
    #: the owner's control plane must keep working in a capped room).
    _room_reply_capped = False
    _cap_why = ""
    if _group_enabled:
        # 044 C2: the room rail is BUILT on the Singular Chat bus — the
        # chat<->session registry (which session a room resolves to), the room
        # caps, the ledger and the outbound router all come from it. With
        # GROUP_CHAT_ENABLED on and SINGULAR_CHAT_ENABLED off (the default, and
        # NOT in the autonomous-mode default set while GROUP_CHAT_ENABLED is),
        # every room mention minted a FRESH unbound session and answered through
        # the raw bot with no [SILENT] rule, no secret scrub and no reply cap.
        # Refuse the room instead of running it half-built; DMs are untouched.
        if container is not None and container.get_service("session_chat_registry") is None:
            _warn_rooms_need_bus(getattr(inbound.identity.source, "surface_id", "?"))
            return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                 reason="rooms_need_singular_chat")
        # 044 C6: `/groups allow here` is the verb that CREATES the allowlist row,
        # and `resolve_access_tier` checks the allowlist BEFORE the owner — so in
        # a not-yet-allowed room the owner's own line was a silent DENIED and the
        # advertised way to allow a room could never work from inside it. Resolve
        # the OWNER principal first, for `/groups` and nothing else: a non-owner
        # is unaffected, turns stay denied, and the only thing that becomes
        # reachable is the owner's room-admin verb. session_id stays None — there
        # is no room session yet, and `/groups` never needs one.
        if _is_groups_verb(text) and _owner_principal_of(user_id):
            return RouteDecision(RouteKind.COMMAND, session_key,
                                 command="/groups", session_id=None)
        try:
            from core.surfaces.access import AccessTier, resolve_access_tier
            tier = resolve_access_tier(container, inbound.identity)
            _note_tier(_trace, tier)
            if tier == AccessTier.DENIED:
                return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                     reason="tier_denied", tier=tier.value)
            # 044 T11: bounded room traffic. `mentioned` is needed by the bot-loop
            # guard below, so it's resolved here rather than after.
            mentioned = inbound.mentions_bot is True
            caps = container.get_service("room_caps") if container else None
            _surf = getattr(inbound.identity.source, "surface_id", "") or ""
            _chat = str(getattr(inbound.identity.source, "chat_id", "") or "")
            _sender = inbound.identity.raw_user_id or user_id
            _is_bot = getattr(inbound, "sender_is_bot", False)
            if caps is not None:
                if _is_bot and not mentioned:
                    return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                         reason="bot_loop_guard", tier=tier.value)
                ok, why = caps.may_trigger(_surf, _chat, _sender, is_bot=_is_bot)
                if not ok:
                    logger.info("route_inbound: room trigger refused (%s) %s:%s",
                               why, _surf, _chat)
                    return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                         reason="room_cap", tier=tier.value)
                # 044 I1: `may_trigger` bounds ONE member (a per-member cooldown
                # and the bot-loop guard); the per-CHAT hourly bound lived only in
                # `may_reply`, at publish time. So N members meant N paid model
                # calls and the cap merely suppressed the output — the room's
                # budget bounded what was SAID, not what was SPENT. Ask the reply
                # cap here too, BEFORE the turn: a room that has nothing left to
                # say has nothing to pay for either. Resolved now, refused at each
                # return site, so an owner COMMAND still gets through.
                _may_reply, _cap_why = caps.may_reply(_surf, _chat)
                _room_reply_capped = not _may_reply
            # 044 T17: `chat.mode` decides whether this line wakes the agent.
            # The legacy GROUP_REQUIRE_MENTION gate is still honoured, but as an
            # ALIAS resolved inside ChatPolicy.defaults() — one gate, not two
            # that can disagree. The owner-facing denial slug is unchanged
            # (`no_mention`): what the owner sees is still "you were not
            # addressed", which is what every mode but `active` means.
            from core.runtime_paths import data_dir_or_home
            from core.surfaces.chat_policy import (load as _load_policy,
                                                   mode_allows_trigger, wake_word_hit)
            _cfg = getattr(container, "config", None) if container else None
            try:
                # The ONE owner-tenant resolver — the overlay's writers
                # (`group_ops._owner_uid`, `owner.py::_group_owner_uid`) read it
                # too, so the room's configured mode is the mode enforced here.
                from core.instance import resolve_owner_user_id
                _owner = resolve_owner_user_id()
            except Exception as e:
                logger.debug("route_inbound: owner tenant unresolved (%s)", e)
                _owner = None
            policy = _load_policy(data_dir_or_home(getattr(_cfg, "data_dir", None)),
                                  _owner or user_id, _surf, _chat)
            role = (inbound.identity.chat_role
                    or ("owner" if tier == AccessTier.OWNER else "member"))
            # 046 + D13: a plain member's slash line is "addressed" only when
            # THIS room granted that verb to members. Resolved from the policy
            # already loaded (never a second read) and intersected with the
            # CLOSED grantable set, so a typo in `chat.member_verbs` can never
            # make an arbitrary verb wake the agent. Without it the grant was
            # reachable only in an `active` room: the mode gate refused the
            # line before the grant was ever consulted.
            _granted_command = False
            if role == "member" and text.startswith("/"):
                _tok = text.split()[0].lower().split("@", 1)[0]
                _granted_command = (
                    _tok in _MEMBER_GRANTABLE_COMMANDS
                    and _tok.lstrip("/") in (policy.member_verbs or ()))
            if not mode_allows_trigger(policy, mentioned=mentioned, role=role,
                                       wake_hit=wake_word_hit(policy, text),
                                       is_command=text.startswith("/"),
                                       granted_command=_granted_command):
                return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                     reason="no_mention", tier=tier.value)
            if tier == AccessTier.GROUP_MEMBER:
                row = None
                try:
                    registry = (container.get_service("session_chat_registry")
                                if container else None)
                    if registry is not None:
                        row = registry.resolve(session_key)
                except Exception as e:
                    logger.debug("group session resolve failed: %s", e)
                    row = None
                sid = row.get("session_id") if row else None
                # The trigger is spent either way — a member who STARTS the room
                # session consumed a turn exactly as one who continues it, so the
                # per-chat cap and per-member cooldown must count both.
                if caps is not None:
                    caps.record_trigger(_surf, _chat, _sender, is_bot=_is_bot)
                # 044 T18 fix round 1 (Critical 1a): an ADMIN's own control-verb
                # line is a COMMAND, never a room turn — `/groups mode|tail`
                # and `/mute` are reachable by him; the lifecycle verbs
                # `/cancel`/`/new`/`/help` too (`_lifecycle_permitted` already
                # lets an admin cancel/restart the room's own session). A
                # plain MEMBER falls through unchanged (his slash line is
                # still a room turn — member verbs are a later, separate
                # schema row).
                if role == "admin" and text.startswith("/"):
                    admin_token = text.split()[0].lower().split("@", 1)[0]
                    if admin_token in _GROUP_ADMIN_COMMANDS:
                        return RouteDecision(RouteKind.COMMAND, session_key,
                                             command=admin_token, session_id=sid)
                # 046: a plain MEMBER's slash line is a COMMAND only when the
                # room's own `chat.member_verbs` grants that verb AND the verb
                # is in the closed grantable set. This is the "later, separate
                # schema row" the admin branch above anticipated: without it
                # `chat.member_verbs` was a key an owner could set and nothing
                # read, and a member's `/mute` was just a line of room chatter.
                #
                # The handler still does its OWN role check, so admitting the
                # line here grants routing, never authority.
                if role == "member" and text.startswith("/"):
                    member_token = text.split()[0].lower().split("@", 1)[0]
                    if (member_token in _MEMBER_GRANTABLE_COMMANDS
                            and _member_verb_granted(container, _surf, _chat,
                                                     member_token)):
                        return RouteDecision(RouteKind.COMMAND, session_key,
                                             command=member_token, session_id=sid)
                # 044 I1: the room has nothing left to say this hour — do not pay
                # for a turn whose reply would be suppressed at publish. Checked
                # AFTER the admin control verbs (a capped room must still be
                # configurable and mutable) and after `record_trigger` (the
                # attempt counts toward the member cooldown either way).
                if _room_reply_capped:
                    logger.info("route_inbound: room turn refused (%s) %s:%s",
                                _cap_why, _surf, _chat)
                    return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                         reason="room_cap", tier=tier.value)
                if sid and not _room_session_is_stale(row, session_key):
                    return RouteDecision(RouteKind.GROUP_TURN,
                                         session_key, session_id=sid)
                # 044 T14: a member may START the room session. It used to be a
                # silent DENIED, so a room the owner had never spoken in answered
                # nobody, ever. The PUBLIC profile (core/surfaces/binding.py) and
                # the room tool gate (core/surfaces/room_policy.py) are what bound
                # what that session can do — not who opened it.
                # 044 I11: this is ALSO where a stale room session resets. The
                # member branch returned GROUP_TURN upstream of the STEER site's
                # boundary policy, so a room session never aged out: one room
                # accumulated a single session forever, growing its history and
                # carrying yesterday's conversation into today's answer, while the
                # owner's own line in the same room DID reset. The cold start's
                # own bind replaces the stale row.
                return RouteDecision(RouteKind.TASK_AGENT, session_key)
            # tier == OWNER -> continue to the legacy flow below, EXCEPT that a
            # warm room session becomes a GROUP_TURN at the STEER site (so the
            # owner's own line also carries the <group-context> block). COMMAND
            # still wins over it — the owner's control plane has to keep working
            # from inside a room — which is why this is a flag, not a return.
            _room_owner_turn = True
            if caps is not None:
                caps.record_trigger(_surf, _chat, _sender, is_bot=_is_bot)
            # 044 I1: the owner's own room TURN is capped too (a room's budget is
            # the room's, not a per-speaker one) — but only the turn. His COMMAND
            # is classified below and is deliberately upstream of this refusal.
            if _room_reply_capped and not text.startswith("/"):
                logger.info("route_inbound: owner room turn refused (%s) %s:%s",
                            _cap_why, _surf, _chat)
                return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                     reason="room_cap", tier=tier.value)
        except Exception as e:
            logger.warning("route_inbound group model fault — failing CLOSED "
                           "to silent DENIED: %s", e)
            return RouteDecision(RouteKind.DENIED, session_key, silent=True,
                                 reason="group_fault")

    # 0b) WS-A THREE-TIER ACCESS MODEL (opt-in CORRESPONDENT_ACCESS_ENABLED, default
    #     OFF -> this whole block is skipped and routing is byte-identical to legacy).
    #     OWNER falls through to the legacy COMMAND/STEER/TASK_AGENT flow; a
    #     CORRESPONDENT is routable ONLY as DATA into the session that contacted them
    #     (a closed tier table -> a correspondent can NEVER reach COMMAND/STEER/
    #     TASK_AGENT); anyone else is DENIED.
    #
    # Reading the flag is fail-OPEN (a config fault must not start denying on a surface
    # that never opted in). But ONCE the model is on, the tier block is fail-CLOSED
    # (Fusion CRITICAL): any fault degrades to DENIED, NEVER falls through to the legacy
    # obey-path — a resolver/registry crash must not turn a gated sender into a steer.
    _corr_enabled = False
    try:
        from core.surfaces.config import SurfaceConfig
        _corr_enabled = SurfaceConfig.correspondent_access_enabled()
    except Exception as e:
        logger.debug("route_inbound tier flag read failed (treat as off): %s", e)
        _corr_enabled = False
    if _corr_enabled:
        try:
            from core.surfaces.access import AccessTier, resolve_access_tier
            thread_id = (getattr(inbound.identity.source, "thread_id", None)
                         or inbound.reply_to)
            tier = resolve_access_tier(container, inbound.identity,
                                       thread_id=thread_id)
            _note_tier(_trace, tier)
            if tier == AccessTier.DENIED:
                return RouteDecision(RouteKind.DENIED, session_key,
                                     reason="tier_denied", tier=tier.value)
            if tier == AccessTier.CORRESPONDENT:
                # The reply belongs to the ORIGINATING session, NOT this chat's key.
                corr = container.get_service("correspondent_registry") if container else None
                row = corr.resolve(
                    surface=inbound.identity.source.surface_id,
                    address=(inbound.identity.raw_user_id or inbound.identity.user_id),
                    thread_id=thread_id,
                ) if corr is not None else None
                orig_session_id = row.get("session_id") if row else None
                if orig_session_id is None:
                    # No originating session to attach to -> do not invent one.
                    return RouteDecision(RouteKind.DENIED, session_key,
                                         reason="no_origin_session",
                                         tier=tier.value)
                return RouteDecision(RouteKind.CORRESPONDENT_DATA, session_key,
                                     session_id=orig_session_id)
            # tier == OWNER -> continue to the legacy flow below.
        except Exception as e:
            # Fail-CLOSED: a fault in the enabled tier path denies, never obeys.
            logger.warning("route_inbound tier model fault — failing CLOSED to DENIED: %s", e)
            return RouteDecision(RouteKind.DENIED, session_key,
                                 reason="tier_fault")
    else:
        # P1-6: the correspondent tier model is OFF. A forgeable-sender network surface
        # (email) must NOT fall through to the legacy obey-path (STEER/TASK_AGENT) — its
        # sender can never be the bound owner in v1 (owner-by-email is off; From: is
        # forgeable), and without the tier model there is no correspondent registry to
        # attach a reply to. Deny here, enforced at the routing boundary so a
        # programmatic EmailHarness or an explicit CORRESPONDENT_ACCESS_ENABLED=false
        # cannot open the obey-path (the CLI `os.environ.setdefault` was only a default).
        surface_id = getattr(getattr(inbound.identity, "source", None), "surface_id", "") or ""
        if surface_id in _FORGEABLE_NETWORK_SURFACES:
            logger.info(
                "route_inbound: %s sender denied — correspondent model off and "
                "owner-by-%s is forgeable (v1 correspondent-or-denied invariant)",
                surface_id, surface_id,
            )
            return RouteDecision(RouteKind.DENIED, session_key,
                                 reason="forgeable_sender")

    # Resolve the bound session row ONCE — used by both COMMAND (so /cancel & /new can
    # act on the running session) and STEER. Fail-open: a lookup error degrades to cold.
    row = None
    try:
        registry = container.get_service("session_chat_registry") if container else None
        if registry is not None:
            row = registry.resolve(session_key)
    except Exception as e:
        logger.debug("route_inbound resolve failed: %s", e)
        row = None

    # 1) COMMAND — control verbs win even over an active session. Carry the bound
    #    session_id so /cancel/ /new actually act on it (was None -> silent no-op).
    if text.startswith("/"):
        # Telegram group syntax sends "/help@MyBot" — strip the @bot suffix so
        # known commands still match (030 L9).
        token = text.split()[0].lower().split("@", 1)[0]
        if token in _COMMANDS or _COMMAND_SHAPE_RE.fullmatch(token):
            return RouteDecision(
                RouteKind.COMMAND, session_key, command=token,
                session_id=(row.get("session_id") if row else None),
            )

    # 2) STEER — a bound (warm) session exists. Sticky: warm never diverts; a
    #    warm-but-dead session is still STEER (the caller rehydrates the key).
    if row:
        # P0.1 boundary policy: continue the SAME session unless it has gone idle or
        # crossed the daily reset hour, in which case the next message starts fresh
        # (TASK_AGENT). Default mode is `idle` since #7 (pin `none` for legacy inert
        # STEER). Fail-open: any policy error keeps STEER, never spuriously wiping a chat.
        try:
            import time as _time
            from core.surfaces.session_policy import should_start_fresh
            from core.surfaces.config import SurfaceConfig
            fresh, _reason = should_start_fresh(
                row, now=_time.time(),
                idle_minutes=SurfaceConfig.session_idle_minutes(),
                daily_hour=SurfaceConfig.session_reset_hour(),
                mode=SurfaceConfig.session_reset_mode(),
            )
            if fresh:
                return RouteDecision(RouteKind.TASK_AGENT, session_key)
        except Exception as e:
            logger.debug("route_inbound boundary policy skipped: %s", e)
        # NOTE: last-activity (updated_at) is bumped on the DELIVERY-success path
        # (TaskAgent.touch_chat_binding from the surface), NOT here — route_inbound stays
        # a side-effect-free decision table.
        if _room_owner_turn:
            # 044 T14: in a room the owner's steer is still a ROOM turn — several
            # humans are present and the agent needs the <group-context> block to
            # know what it is answering. The cold start stays TASK_AGENT (there is
            # no session to carry an ephemeral context block into yet).
            return RouteDecision(RouteKind.GROUP_TURN, session_key,
                                 session_id=row.get("session_id"))
        return RouteDecision(RouteKind.STEER, session_key, session_id=row.get("session_id"))

    # 3) cold — optional ChatAgent fast-path (default-OFF cost optimization).
    if is_chitchat is not None:
        from core.surfaces.config import SurfaceConfig
        if SurfaceConfig.chat_intent_classifier_enabled():
            try:
                verdict = is_chitchat(inbound)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
                if verdict:
                    return RouteDecision(RouteKind.CHAT_FASTPATH, session_key)
            except Exception as e:  # fail-open to the Task agent
                logger.debug("route_inbound is_chitchat failed: %s", e)

    return RouteDecision(RouteKind.TASK_AGENT, session_key)
