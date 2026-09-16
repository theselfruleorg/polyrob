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
