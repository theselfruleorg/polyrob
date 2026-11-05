"""``recent_activity scope=actions``: the action ledger past the prompt's cap (103)."""
from typing import Any


def recent_actions_result(execution_context: Any, controller: Any) -> Any:
    from core.autonomy_marker import is_autonomous
    from core.security.untrusted_wrap import wrap_untrusted
    from core.session_ledger import LEDGER_READ_MAX_CHARS, read_ledger, render
    from agents.task.loop_types.controller_types import ActionResult
    user_id = getattr(execution_context, "user_id", None) or getattr(controller, "user_id", None)
    session_id = getattr(execution_context, "session_id", None) or ""
    # An autonomous run reads its own run, an owner chat every act of the owner.
    ledger = read_ledger(user_id, session_id=session_id if is_autonomous(session_id) else "")
    body = render(ledger, max_chars=LEDGER_READ_MAX_CHARS)
    return ActionResult(
        extracted_content=(wrap_untrusted("recent_activity", body) if body
                           else "No cards decided or outward acts in the last 48 h."),
        include_in_memory=True)
