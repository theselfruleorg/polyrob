"""``recent_activity`` scopes that read a store other than episodic memory.

* ``scope=actions`` — the action ledger past the prompt's cap (103).
* ``scope=recap`` — the owner's ``/recap`` text (120 W7), from the ONE reader
  ``core.recap.recap_reply``. The recap carries the treasury rollup and what the
  agent told the owner, so only the owner's tenant reads it, and the text (task
  lines, delivered messages) reaches the model framed as data.
"""
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


def recap_result(params: Any, execution_context: Any, controller: Any) -> Any:
    """``recent_activity(scope='recap')``: the ``/recap`` text over ``since``."""
    from agents.task.loop_types.controller_types import ActionResult
    from core.instance import is_owner_uid
    from core.recap import recap_reply
    from core.runtime_paths import container_data_home
    from core.security.untrusted_wrap import wrap_untrusted
    user_id = str(getattr(execution_context, "user_id", None)
                  or getattr(controller, "user_id", None) or "")
    if not user_id or not is_owner_uid(user_id):
        return ActionResult(error="recap refused: the recap is the owner's (it carries the "
                                  "treasury rollup); this turn is not the owner's tenant",
                            include_in_memory=True)
    text = recap_reply(user_id, container_data_home(getattr(controller, "container", None)),
                       str(getattr(params, "since", None) or "24h"))
    return ActionResult(extracted_content=wrap_untrusted("recap", text), include_in_memory=True)


async def recent_scope_result(params: Any, execution_context: Any, controller: Any) -> Any:
    """Dispatch the scope; the recap reads several stores, off the event loop."""
    if params.scope == "recap":
        import asyncio
        return await asyncio.to_thread(recap_result, params, execution_context, controller)
    return recent_actions_result(execution_context, controller)
