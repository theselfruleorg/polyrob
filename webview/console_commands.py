"""030 WS-B6: console chat-box slash-verbs route through the shared owner-verb
plane (extracted from server.py per the god-file ratchet)."""
import logging

logger = logging.getLogger(__name__)

#: 043 A10/A45: these two verbs are session-creating — the console has its own
#: UI for them (see the `/task`/`/new` short-circuit below) — so they are
#: filtered out of the console's rendered `/help` body rather than advertised
#: as chat-box commands nobody can actually run there.
CONSOLE_HELP_EXCLUDED = ("/task", "/new")

#: Answer for `/help task` / `/help new` on the console — ONE sentence so the
#: wording can't drift between the two verbs.
_CONSOLE_HELP_UNAVAILABLE = (
    "not available on this seat — the console has its own controls for that"
)


def _filter_console_help(body: str) -> str:
    """Drop any `_HELP_BODY` line that starts with an excluded verb."""
    lines = [ln for ln in body.splitlines()
             if not any(ln.startswith(v + " ") or ln.startswith(v + "\n") or
                        ln.split(" ")[0] == v for v in CONSOLE_HELP_EXCLUDED)]
    return "\n".join(lines)


#: The feed event type the console transcript draws as a console answer (a
#: `.note`, not an agent bubble) — `transcript.js` maps it to `_commandNote`.
COMMAND_REPLY_EVENT = "command_reply"


def _console_deliver(task_agent, clean_id: str, user_id: str):
    """The ``deliver`` for a console verb that runs in the background.

    Two legs, both fail-open, so the result is never lost:

    1. the OWNER NOTICE rail (``core.surfaces.user_delivery.deliver_user_message``)
       — the one durable path: it reaches the owner's chat when one is bound
       and is otherwise recorded as an ``owner_notice`` (``/missed``, Work ›
       Log). Critical lane: this is the outcome of the owner's own money act,
       and the daily cap must not starve it.
    2. a live ``feed_update`` to this chat's Socket.IO room, so the console
       tab that typed the verb shows the answer where it asked. Skipped on the
       cold-open path (no session, ``clean_id == ""``).
    """
    from agents.task.path import is_reserved_session_id
    seat = bool(clean_id) and is_reserved_session_id(clean_id)

    async def _deliver(text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        try:
            from core.surfaces.user_delivery import deliver_user_message
            await deliver_user_message(
                getattr(task_agent, "container", None), user_id, body,
                source="console_command",
                session_id=(None if seat else clean_id) or None,
                priority="critical")
        except Exception:
            logger.warning("console command: owner-notice delivery failed",
                           exc_info=True)
        if not clean_id or seat:
            # A console SEAT ("money"/"inbox") is not a session: no tab listens
            # on a session room of that name, and a session that once took the
            # name must never receive the owner's money result (WEB-1).
            return
        try:
            import time

            import webview.server as _srv
            event = {"type": COMMAND_REPLY_EVENT, "timestamp": time.time(),
                     "data": {"text": body}}
            from webview.session_access import session_room
            await _srv._sio.emit("feed_update", event, room=session_room(clean_id))
        except Exception:
            logger.debug("console command: live feed push skipped", exc_info=True)
    return _deliver


async def maybe_handle_console_command(task_agent, clean_id: str, user_id: str, text: str):
    """Returns the reply text for a slash verb, or None for anything that
    should reach the agent as a normal message.

    - Not a slash line, or the line cannot even be classified: None (fail-open
      — never swallow a user message over an error here).
    - A command-SHAPED token no verb owns (``/statsu``): the same "unknown
      command" help answer Telegram gives, never an agent turn (audit CLI11).
    - A KNOWN verb whose routing raises: an error reply, never the verb line
      forwarded to the agent as prose — ``/send … go`` must not become a
      message the model reads as an instruction (audit CLI12).
    """
    try:
        token = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
        if not token.startswith("/"):
            return None
        token = token.split("@", 1)[0]
        from core.surfaces.dispatcher import _COMMAND_SHAPE_RE, command_names
        from core.surfaces.tappable import is_tappable_token
        known = token in command_names() or is_tappable_token(token)
        shaped = bool(_COMMAND_SHAPE_RE.fullmatch(token))
    except Exception:
        logger.debug("console command classification skipped (fail-open)", exc_info=True)
        return None
    if token in ("/task", "/new"):
        return None  # session-creating verbs: the console has real UI for these
    if task_agent is None:
        return None  # two-service shape: let the :9000 proxy carry it
    if not known:
        if not shaped:
            return None  # a path or prose that starts with "/": the agent's
        return _unknown_verb_reply(token)
    try:
        return await _route_known_verb(task_agent, clean_id, user_id, text, token)
    except Exception as exc:
        logger.warning("console command %s failed in routing", token, exc_info=True)
        return _routing_failed_reply(token, exc)


def _unknown_verb_reply(token: str) -> str:
    """The Telegram seat's own answer for an unknown verb (one source)."""
    try:
        from surfaces.telegram.harness import _unknown_command_text
        return _unknown_command_text(token)
    except Exception:
        logger.debug("unknown-verb help unavailable", exc_info=True)
        return f"Unknown command {token}. Send /help for the full list."


def _routing_failed_reply(token: str, exc: BaseException) -> str:
    return (f"{token} could not run here ({type(exc).__name__}). The line was "
            "not sent to the agent. Try again, or use Telegram or the terminal.")


async def _route_known_verb(task_agent, clean_id: str, user_id: str, text: str,
                            token: str):
    """Run a KNOWN owner verb through the shared owner-verb plane."""
    args = (text or "").strip().split()[1:]
    if token == "/help" and args and ("/" + args[0].strip().lstrip("/").lower()
                                      in CONSOLE_HELP_EXCLUDED):
        return _CONSOLE_HELP_UNAVAILABLE
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.harness import _handle_command
    from surfaces.telegram.inbound import InboundResult
    source = SessionSource(surface_id="webview", chat_id=clean_id, chat_type="dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id=str(user_id),
                                                          source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND,
                             # A10: the tail must match build_session_key's real
                             # DM shape (…:dm:{chat_id}:{user_id}) — the lifecycle
                             # gate for /cancel and /new (harness._lifecycle_permitted)
                             # confirms a non-owner is acting on THEIR OWN session by
                             # matching this suffix; the route above already checked
                             # `current_user_id == session_owner_id` before this is
                             # ever reached, so this is restating an already-verified
                             # fact, not a new grant.
                             session_key=f"agent:main:webview:dm:{clean_id}:{user_id}",
                             session_id=clean_id, command=token)
    result = InboundResult(inbound=inbound, decision=decision)
    # 061: a DECIDING console verb (/approve, /pause, …) is a line of the
    # owner's conversation exactly as the same verb on Telegram is.
    from surfaces.telegram.harness import _record_owner_line
    _record_owner_line(task_agent, result, session_id=clean_id, kind="command")
    # An EXECUTING money verb (`/send … go`, `/bridge … go`) waits up to
    # ~120 s for a receipt. Without a `deliver` the harness ran it INLINE
    # and held this HTTP request the whole time. The console passes one
    # under the SAME rule Telegram uses (`_runs_in_background`), so the
    # harness answers "started" at once and hands the result here. Only
    # then: every other verb keeps its inline answer, byte-identical.
    from surfaces.telegram.harness import _runs_in_background
    # A card tap (`/card_<id>_ok`) may run a money line: the card seat
    # decides whether THAT line goes to the background, so it gets one too.
    from core.surfaces.cards import parse_card_token
    deliver = (_console_deliver(task_agent, clean_id, str(user_id))
               if (_runs_in_background(token, result)
                   or parse_card_token(token)[0] is not None) else None)
    reply = await _handle_command(task_agent, result, spawn=None,
                                  deliver=deliver)
    if token == "/help" and not args and isinstance(reply, str):
        reply = _filter_console_help(reply)
    # An action card (or a room reply) answers with a CommandReply; the
    # console draws text, and the text keeps every tap token.
    from core.surfaces.command_reply import CommandReply, reply_text
    if isinstance(reply, CommandReply):
        reply = reply_text(reply)
    return reply


async def run_console_line(task_agent, clean_id: str, user_id: str, text: str) -> str:
    """Run one owner line (a verb or a tap token) as the console chat box
    would, and return its answer text ("" when it is not a console verb or no
    agent runs in this process). The Inbox's card buttons post through this."""
    reply = await maybe_handle_console_command(task_agent, clean_id, user_id, text)
    return "" if reply is None else str(reply)


# --- the cold-open short-circuit (043 A17) ---------------------------------- #
#
# ⚠️ A slash verb typed into an EMPTY chat box created a SESSION. The verb check
# above ran only on ``POST /api/session/{id}/messages`` — the bound-chat path —
# so ``/halt`` from the console's front door did not halt: it started an agent
# run whose task was the literal text "/halt". This closes that door by owning
# ``POST /api/task/sessions`` one hop BEFORE the api-tier route of the same
# path, answering a known verb inline and handing everything else to the real
# creator unchanged.


def looks_like_console_verb(text) -> bool:
    """Is *text*'s first token a verb (known, or command-shaped) this seat
    answers inline?

    Pure and cheap: the membership test only, no agent, no I/O. ``/task`` and
    ``/new`` are excluded for the same reason as above — the console has its
    own controls for starting work, and routing them here would make creating
    a session impossible.
    """
    token = (str(text or "").strip().split() or [""])[0].lower()
    if not token.startswith("/"):
        return False
    token = token.split("@", 1)[0]
    try:
        from core.surfaces.dispatcher import _COMMAND_SHAPE_RE, command_names
    except Exception:  # pragma: no cover — the dispatcher is always importable
        return False
    from core.surfaces.tappable import is_tappable_token
    # A command-SHAPED unknown verb is answered too (the help reply, audit
    # CLI11): a typo in an empty chat box must not start a session. A
    # MALFORMED tap token (``/card_nothex_ok``, ``/approve_zz``) is not: its
    # base is a real verb, and the tap parser already refused it.
    if token in ("/task", "/new"):
        return False
    if token in command_names() or is_tappable_token(token):
        return True
    base = token.split("_", 1)[0]
    malformed_tap = "_" in token and (base in command_names() or base == "/card")
    return bool(_COMMAND_SHAPE_RE.fullmatch(token)) and not malformed_tap


def build_console_create_router():
    """A router owning ``POST /task/sessions``, to mount under ``/api`` FIRST.

    Route order decides: FastAPI takes the first path match, so this must be
    included BEFORE ``api.task_http_api.router``. A request whose task is not a
    known verb is passed straight to that router's own ``create_session`` — the
    same function, the same dependency, the same body — so this adds a hop and
    changes no creation behaviour.

    The imports are LOCAL to the call so a console whose api tier failed to
    import simply never mounts this router, exactly like the task router it
    fronts.
    """
    from typing import Any, Dict

    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse

    from api.task_http_api import create_session, get_task_agent
    from webview import webgate

    router = APIRouter()

    # The guard rides on the ROUTE, not only on the mount: the read-only
    # ratchet reads decorators, and a route whose only gate is an
    # ``include_router`` argument is one refactor away from having none.
    @router.post("/task/sessions", dependencies=webgate.MUTATION_DEPS)
    async def console_create_session(request_body: Dict[str, Any], req: Request,
                                     agent=Depends(get_task_agent)):
        task = (request_body or {}).get("task")
        if looks_like_console_verb(task):
            # The tenant comes from the ONE console resolver, which 403s a
            # multitenant caller with no identity and an unbound own_ops
            # console — an owner verb run as nobody is not an owner verb.
            from webview.pages import _effective_user_id
            reply = await maybe_handle_console_command(
                agent, "", str(_effective_user_id(req)), str(task))
            if reply is not None:
                # No session created, and the seat says so in the one field the
                # bound-chat path already answers with.
                return JSONResponse({"success": True, "command_reply": reply})
        return await create_session(request_body, req, agent)

    return router
