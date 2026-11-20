"""Validate tenant overrides against the existing session defaults."""
from contextlib import asynccontextmanager, contextmanager

from core.billing_context import billed_tool_refusal, is_billed
from core.config_policy.goal_flags import interactive_max_steps


def billed_step_ceiling() -> int:
    """The most steps a billed request may ask for: the interactive default.
    Never raised for autonomous runs (083 WS-4). Read at use time (098 CO-09)."""
    return interactive_max_steps()


def clamp_autonomous_billed_steps(request, session_id) -> None:
    """083 WS-4: a credit-billed tenant's goal/cron run keeps the billed
    ceiling — the operator's ``AUTONOMOUS_MAX_STEPS`` (150) is clamped to it
    here instead of refusing the run. An interactive request is untouched
    (it is refused above the ceiling, as before)."""
    from agents.task.session_class import is_autonomous_session
    if not is_autonomous_session(session_id):
        return
    ceiling = billed_step_ceiling()
    if isinstance(request.max_steps, int) and request.max_steps > ceiling:
        request.max_steps = ceiling


def validate_billed_request(request, *, required=False, session_id=None) -> None:
    if not (required or is_billed()):
        return
    if session_id is not None:
        clamp_autonomous_billed_steps(request, session_id)
    from modules.llm.billing_guard import require_billable_model
    require_billable_model(request.model, request.provider)
    ceiling = billed_step_ceiling()
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


def validate_billed_session(info, *, inbound_line: bool = False) -> None:
    """Old or operator-created sessions cannot become unrestricted tenant compute.

    ``inbound_line``: an API is adding a line. A room admin's session takes lines only
    from that admin's /admin: the room gate reads their line and taint, so this line would
    run under their name (101). ⚠️ Only then: ``billed_session`` calls this for EVERY run,
    and a run of that session must pass."""
    if inbound_line and ((info or {}).get("request") or {}).get("room_admin"):
        raise ValueError("This is a room admin's session. It takes lines only from /admin.")
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


async def bind_request_limits(container, request, user_id, session_id) -> bool:
    """The ONE limit check of a new session (125 S33, D11): ``/api``, A2A and the
    lite agent all reach it through ``create_session``.

    A billed tenant is validated as before (an out-of-range ``max_steps`` raises).
    Every other tenant that is not the owner and not admin tier has ``max_steps``
    CLAMPED (no error) to the interactive budget, or to the autonomous cap for an
    autonomous session — a box with no credit billing no longer lets a stranger ask
    for 100000 steps. A missing tier manager cannot grant the exemption.

    Returns whether the request is credit-billed (the session metadata stamp)."""
    credit_billed = await credit_billing_required(container, user_id)
    validate_billed_request(request, required=credit_billed, session_id=session_id)
    if credit_billed or is_billed():
        return credit_billed
    from core.surfaces.owner_address import is_owner_tenant
    if is_owner_tenant(user_id):
        return False
    tier_manager = container.get_service('tier_manager') if container is not None else None
    if tier_manager is not None:
        try:
            if await tier_manager.get_user_tier(user_id) == 'admin':
                return False
        except Exception:
            pass  # an unreadable tier grants nothing: clamp
    from agents.task.session_class import is_autonomous_session
    if session_id and is_autonomous_session(session_id):
        from core.config_policy import AutonomyConfig
        ceiling = AutonomyConfig.autonomous_max_steps()
    else:
        ceiling = interactive_max_steps()
    steps = request.max_steps
    try:
        steps = int(steps) if not isinstance(steps, bool) else ceiling
    except (TypeError, ValueError):
        steps = ceiling
    request.max_steps = min(max(steps, 1), ceiling)
    return False


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
