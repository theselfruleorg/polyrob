"""Validate tenant overrides against the existing session defaults."""
from contextlib import asynccontextmanager, contextmanager

from core.billing_context import billed_tool_refusal, is_billed


def validate_billed_request(request, *, required=False) -> None:
    if not (required or is_billed()):
        return
    from modules.llm.billing_guard import require_billable_model
    require_billable_model(request.model, request.provider)
    ceiling = request.__dataclass_fields__["max_steps"].default
    steps = request.max_steps
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= ceiling:
        raise ValueError(f"Billed requests require max_steps between 1 and {ceiling}")
    if not isinstance(request.tools, list) or any(not isinstance(t, str) for t in request.tools):
        raise ValueError("tools must be a list of tool IDs")
    for tool in request.tools:
        refusal = billed_tool_refusal(tool)
        if refusal:
            raise ValueError(refusal)
    if request.session_config and request.session_config.get("tools_config"):
        raise ValueError("Billed requests cannot override operator tool configuration")


@contextmanager
def billed_session(info, balance_manager=None):
    """Restore a saved tenant restriction without leaking it to later caller work."""
    from core.billing_context import mark_billed, reset_billed
    validate_billed_session(info)
    token = None
    if info and info.get("billing_limited") is True:
        from modules.credits.reservations import credit_reserver
        reserve = credit_reserver(balance_manager, info.get("user_id"), info.get("session_id")) \
            if balance_manager is not None else None
        token = mark_billed(reserve)
    try:
        yield
    finally:
        if token is not None:
            reset_billed(token)


def validate_billed_session(info) -> None:
    """Old or operator-created sessions cannot become unrestricted tenant compute."""
    if is_billed() and (not info or info.get("billing_limited") is not True):
        raise ValueError("This session has no verified billing limits; create a new session")


async def credit_billing_required(container, user_id) -> bool:
    """Resolve credit liability independently of the caller's admission shortcut."""
    from core.billing_context import is_prepaid
    if container is None or container.get_service('balance_manager') is None or is_prepaid():
        return False
    # The instance owner runs on the operator's own compute: never a billed
    # tenant (an owner seeded at tier 'free' would otherwise be held to the
    # tenant tool profile and credit holds). A billed HTTP admission still
    # binds through is_billed() and the saved billing_limited stamp.
    from core.surfaces.owner_address import is_owner_tenant
    if is_owner_tenant(user_id):
        return False
    tier_manager = container.get_service('tier_manager')
    # Missing tier information cannot grant a billing exemption.
    return tier_manager is None or await tier_manager.get_user_tier(user_id) != 'admin'


@asynccontextmanager
async def session_compute(info, user_id, container):
    """Fresh liability and saved limits also bind non-HTTP chat continuations."""
    from core.inference_context import inference_user
    if await credit_billing_required(container, user_id):
        if not info or info.get('billing_limited') is not True:
            raise ValueError("This session has no verified billing limits; create a new session")
    balance = container.get_service('balance_manager') if container is not None else None
    with inference_user(user_id), billed_session(info, balance):
        yield
