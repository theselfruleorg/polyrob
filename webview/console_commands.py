"""030 WS-B6: console chat-box slash-verbs route through the shared owner-verb
plane (extracted from server.py per the god-file ratchet)."""
import logging

logger = logging.getLogger(__name__)


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
        from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
        from surfaces.telegram.harness import _handle_command
        from surfaces.telegram.inbound import InboundResult
        source = SessionSource(surface_id="webview", chat_id=clean_id, chat_type="dm")
        inbound = InboundMessage(text=text, identity=Identity(user_id=str(user_id),
                                                              source=source))
        decision = RouteDecision(kind=RouteKind.COMMAND,
                                 session_key=f"agent:main:webview:dm:{clean_id}",
                                 session_id=clean_id, command=token)
        return await _handle_command(task_agent,
                                     InboundResult(inbound=inbound, decision=decision),
                                     spawn=None)
    except Exception:
        logger.debug("console command routing skipped (fail-open)", exc_info=True)
        return None
