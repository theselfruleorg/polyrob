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


async def maybe_handle_console_command(task_agent, clean_id: str, user_id: str, text: str):
    """Returns the reply text for a known owner verb, or None for anything that
    should reach the agent as a normal message. Fail-open — never swallow a
    user message over an error here."""
    try:
        token = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
        if not token.startswith("/"):
            return None
        token = token.split("@", 1)[0]
        from core.surfaces.dispatcher import _COMMANDS, RouteDecision, RouteKind
        if token not in _COMMANDS:
            return None  # unknown slash: let the agent see it (prose question)
        if token in ("/task", "/new"):
            return None  # session-creating verbs: the console has real UI for these
        if task_agent is None:
            return None  # two-service shape: let the :9000 proxy carry it
        args = (text or "").strip().split()[1:]
        if token == "/help" and args and ("/" + args[0].strip().lstrip("/").lower()
                                          in CONSOLE_HELP_EXCLUDED):
            return _CONSOLE_HELP_UNAVAILABLE
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
        reply = await _handle_command(task_agent,
                                      InboundResult(inbound=inbound, decision=decision),
                                      spawn=None)
        if token == "/help" and not args and isinstance(reply, str):
            reply = _filter_console_help(reply)
        return reply
    except Exception:
        logger.debug("console command routing skipped (fail-open)", exc_info=True)
        return None


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
    """Is *text*'s first token an owner verb this seat can answer inline?

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
        from core.surfaces.dispatcher import _COMMANDS
    except Exception:  # pragma: no cover — the dispatcher is always importable
        return False
    return token in _COMMANDS and token not in ("/task", "/new")


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
