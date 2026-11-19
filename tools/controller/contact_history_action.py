"""The read-only ``contact_history`` action — who the agent wrote to, the state of
each contact, and the transcript with one of them.

Extracted from ``action_registration.py`` (its size ceiling) in 120 W5. The read
is ``core.surfaces.contacts_view.contacts_text``: the same text the owner's
``/contacts`` prints on every seat, with the agent's own hint lines. The listing
joins the correspondent registry: ``active``, ``pending`` (waits for the owner),
``expired`` (rejected or timed out), or no binding.

⚠️ Read-only. Approving or rejecting a contact stays the owner's (``/pending``).

⚠️ The listing and the transcript carry a third party's words and addresses, so
both reach the model framed as untrusted data.

⚠️ Registry-closure landmine: NO ``from __future__ import annotations`` here —
the registry introspects the closure's first-param annotation.
"""
from agents.task.agent.views import ActionResult

_MAX_LIMIT = 50


def register_contact_history_action(controller) -> None:
    """Register ``contact_history``. Gated CORRESPONDENT_ACCESS_ENABLED: with no
    correspondent model there are no contacts to read."""
    try:
        from core.surfaces.config import SurfaceConfig
        if not SurfaceConfig.correspondent_access_enabled():
            return
    except Exception:
        return

    from tools.controller.views import ContactHistoryAction

    @controller.registry.action(
        'Show the durable conversation history with an external correspondent '
        '(what we sent and what they replied, across sessions). Omit '
        'surface+address to LIST all conversations with each contact\'s state '
        '(active, pending, expired). Read-only.',
        param_model=ContactHistoryAction,
    )
    async def contact_history(params: ContactHistoryAction, execution_context=None) -> ActionResult:
        user_id = (getattr(execution_context, 'user_id', None)
                   or getattr(controller, 'user_id', None) or "")
        container = getattr(controller, "container", None)
        from core.runtime_paths import container_data_home
        from core.surfaces.contacts_view import contacts_text
        limit = max(1, min(int(params.limit or 10), _MAX_LIMIT))
        text = contacts_text(
            str(user_id), container_data_home(container) or "",
            params.surface or "", params.address or "", container=container,
            list_limit=limit, history_limit=limit, max_chars=4000,
            one_hint="One transcript: contact_history(surface=..., address=...).",
            list_hint="contact_history with no address lists the ones I have",
            frame_listing=True)
        return ActionResult(extracted_content=text, include_in_memory=True)
