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
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from core.surfaces.envelopes import InboundMessage
from core.surfaces.session_chat_registry import build_session_key

logger = logging.getLogger(__name__)

# Owner-admin verbs (§7.1/§7.2b, owner-UX P4 T2): the phone-only headless owner's
# surface for the self-evolution approve loop (/pending /approve /reject), open
# asks (/asks /fulfill), the outbound-messaging allowlist (/allow /deny
# /allowlist), and the read-only status/recap/goals/prefs verbs (/status /recap
# /goals /prefs). The surface handler owner-gates them by principal; routing here
# only classifies them as COMMAND so they win over an active session.
_COMMANDS = ("/task", "/cancel", "/new", "/help",
             "/pending", "/approve", "/reject", "/asks", "/fulfill",
             "/allow", "/deny", "/allowlist",
             "/status", "/recap", "/journey", "/goals", "/prefs", "/config",
             "/kb", "/files")

ChitchatPredicate = Callable[[InboundMessage], Union[bool, Awaitable[bool]]]


# P1-6: forgeable-sender network surfaces whose senders can NEVER be the bound owner
# in v1 (the From:/address is trivially spoofable). Such a surface must never fall
# through to the legacy obey-path when the correspondent tier model is off — it is
# correspondent-or-denied by construction. Kept separate from access._LOCAL_OWNER_
# SURFACES (its inverse): local surfaces get the owner bypass, these are refused it.
_FORGEABLE_NETWORK_SURFACES = {"email"}


class RouteKind(str, Enum):
    COMMAND = "command"
    STEER = "steer"
    CHAT_FASTPATH = "chat_fastpath"
    TASK_AGENT = "task_agent"
    DENIED = "denied"  # polyrob D3: ingress blocked (pairing required / not paired)
    CORRESPONDENT_DATA = "correspondent_data"  # WS-A: third-party reply -> DATA into
                                               # the originating session (never a command)


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


async def route_inbound(
    container: Any,
    inbound: InboundMessage,
    *,
    is_chitchat: Optional[ChitchatPredicate] = None,
) -> RouteDecision:
    user_id = inbound.identity.user_id
    session_key = build_session_key(inbound.identity.source, user_id)
    text = (inbound.text or "").strip()

    # 0) ACCESS GATE (polyrob D3) — when POLYROB_REQUIRE_PAIRING is on, an unpaired
    #    non-owner is denied (and issued a pairing code). Fail-open + default-off, so
    #    this is byte-identical until an operator opts into pairing.
    try:
        from core.pairing import guard_inbound
        surface_id = getattr(inbound.identity.source, "surface_id", None)
        denial = guard_inbound(container, user_id, surface_id=surface_id)
        if denial is not None:
            return RouteDecision(RouteKind.DENIED, session_key,
                                 pairing_code=denial.pairing_code)
    except Exception as e:  # never block routing on a guard fault
        logger.debug("route_inbound access-gate skipped: %s", e)

    # 0a-groups) W3 GROUP CHAT (opt-in GROUP_CHAT_ENABLED, default OFF). In an
    #    allowlisted group chat: the owner gets the legacy flow (mention-gated);
    #    a participant's message is DATA into the bound group session
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
            from agents.task.surface_config import SurfaceConfig as _SC
            _group_enabled = _SC.group_chat_enabled()
        except Exception as e:
            logger.debug("route_inbound group flag read failed (treat as off): %s", e)
            _group_enabled = False
        if not _group_enabled:
            logger.info(
                "route_inbound: %s %s message denied — GROUP_CHAT_ENABLED off",
                getattr(inbound.identity.source, "surface_id", "?"), _chat_type,
            )
            return RouteDecision(RouteKind.DENIED, session_key, silent=True)
    if _group_enabled:
        try:
            from core.surfaces.access import AccessTier, resolve_access_tier
            tier = resolve_access_tier(container, inbound.identity)
            if tier == AccessTier.DENIED:
                return RouteDecision(RouteKind.DENIED, session_key, silent=True)
            require_mention = True
            try:
                from agents.task.surface_config import SurfaceConfig as _SC
                require_mention = _SC.group_require_mention()
            except Exception:
                require_mention = True
            mentioned = inbound.mentions_bot is True
            if require_mention and not mentioned:
                return RouteDecision(RouteKind.DENIED, session_key, silent=True)
            if tier == AccessTier.GROUP_PARTICIPANT:
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
                if sid:
                    return RouteDecision(RouteKind.CORRESPONDENT_DATA,
                                         session_key, session_id=sid)
                # Participants can never START a session — silent ignore.
                return RouteDecision(RouteKind.DENIED, session_key, silent=True)
            # tier == OWNER -> continue to the legacy flow below.
        except Exception as e:
            logger.warning("route_inbound group model fault — failing CLOSED "
                           "to silent DENIED: %s", e)
            return RouteDecision(RouteKind.DENIED, session_key, silent=True)

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
        from agents.task.surface_config import SurfaceConfig
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
            if tier == AccessTier.DENIED:
                return RouteDecision(RouteKind.DENIED, session_key)
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
                    return RouteDecision(RouteKind.DENIED, session_key)
                return RouteDecision(RouteKind.CORRESPONDENT_DATA, session_key,
                                     session_id=orig_session_id)
            # tier == OWNER -> continue to the legacy flow below.
        except Exception as e:
            # Fail-CLOSED: a fault in the enabled tier path denies, never obeys.
            logger.warning("route_inbound tier model fault — failing CLOSED to DENIED: %s", e)
            return RouteDecision(RouteKind.DENIED, session_key)
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
            return RouteDecision(RouteKind.DENIED, session_key)

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
        token = text.split()[0].lower()
        if token in _COMMANDS:
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
            from agents.task.surface_config import SurfaceConfig
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
        return RouteDecision(RouteKind.STEER, session_key, session_id=row.get("session_id"))

    # 3) cold — optional ChatAgent fast-path (default-OFF cost optimization).
    if is_chitchat is not None:
        from agents.task.surface_config import SurfaceConfig
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
