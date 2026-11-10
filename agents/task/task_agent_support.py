"""Module-level helpers shared by ``TaskAgent`` and its mixins (S8 split, 2026-08-29).

Moved verbatim out of ``agents/task_agent_lite.py`` so the chat / delivery / lifecycle
mixins can import them without a cycle; ``task_agent_lite`` re-exports every name, so
``from agents.task_agent_lite import SessionRequest`` (and the test seams
``_spawn_detached`` / ``_DETACHED_TASKS`` / ``_SELF_WAKE_TASKS``) keep working on the SAME
objects.
"""
from agents.task.tool_defaults import default_session_tools
from core.optional_extras import missing_extra_hint
from core.config_policy.goal_flags import interactive_max_steps
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Union, List
import asyncio
import logging

logger = logging.getLogger(__name__)


def task_unavailable_message(reason: Optional[str]) -> str:
    """Format the task-package failure with its real cause and, when the
    missing module maps to a pip extra, the install remedy (proposal 027 —
    the bare sentinel left wheel users with no way to learn the fix)."""
    msg = "Task package not available"
    if reason:
        msg += f": {reason}"
        hint = missing_extra_hint(reason)
        if hint:
            msg += f" ({hint})"
    return msg
# Session limits per user
# Defensive fallback ONLY for the getattr below; the runtime SSOT is the config
# field BotConfig.max_sessions_per_user (core/config.py, default 10). Keep this in
# sync with that default. (B3: was previously shadowed by a stray, unused
# constants.MAX_SESSIONS_PER_USER=100 — removed.)
MAX_SESSIONS_PER_USER = 10
#: Strong references to in-flight fire-and-forget self-wake ``run_session`` tasks
#: (AU-F3.1). asyncio only holds a WEAK reference to a task created via
#: ``asyncio.create_task`` -- without this, the task object can be garbage-collected
#: mid-run (a well-known asyncio footgun), silently dropping the self-wake dispatch.
#: Mirrors ``polyrob/app/autonomy_runtime.py::_BACKGROUND_TASKS``. Self-cleans via
#: ``add_done_callback``.
_SELF_WAKE_TASKS: set = set()
#: Strong refs to other fire-and-forget tasks (background run_session resumes,
#: orchestrator cleanup). Same asyncio weak-ref footgun as _SELF_WAKE_TASKS
#: (P1 finalization: several bare ``asyncio.create_task(...)`` sites held no ref,
#: so the task could be GC'd mid-run and silently dropped).
_DETACHED_TASKS: set = set()
def _spawn_detached(coro):
    """Schedule ``coro`` as a fire-and-forget task WITH a strong reference held
    until it finishes, so the event loop can't GC it mid-run."""
    from core.async_bridge import spawn_retained
    return spawn_retained(coro, _DETACHED_TASKS)
def _resolve_session_runtime(provider=None, model=None, env=None):
    """Fill missing SessionRequest provider/model from the operator's runtime
    config. Thin alias for the one agents-tier resolver in
    ``agents.task.config`` (kept as the import name existing callers use)."""
    from agents.task.config import resolve_session_provider_model
    return resolve_session_provider_model(provider, model, env=env)
@dataclass
class SessionRequest:
    """Model for session configuration."""
    task: str
    model: Optional[str] = None
    provider: Optional[str] = None
    tools: List[str] = None
    max_steps: int = field(default_factory=interactive_max_steps)  # 098 CO-09: read at use
    temperature: float = 0.0
    use_vision: bool = True
    session_config: Optional[Dict[str, Any]] = None
    #: 060 WS-5: skill ids the rail pins (`payload.skills`) — seeded into the
    #: session regardless of keyword matching. None = today's matching only.
    skills: Optional[List[str]] = None
    #: 025: the goal this session serves and its memory scope (label + regime),
    #: minted by the dispatcher / cron runner — never by the agent. None = shared.
    goal_id: Optional[str] = None
    memory_scope: Optional[str] = None
    memory_regime: Optional[str] = None
    #: 068 G2: the run's declared buy target (``core.wallet.buy_target``), normalized
    #: by the cron runner / goal dispatcher from ``payload.target_token``.
    money_target: Optional[Dict[str, str]] = None
    #: 086 W2: the run's trading mandate id (``payload.mandate``); the money kernel's
    #: mandate step reads it from the execution context. None = not a mandate run.
    mandate: Optional[str] = None
    #: 101: ``{surface, chat_id, uid, user_id, name}`` of a room+admin session, minted
    #: by the harness from the routed sender. None = an ordinary room session.
    room_admin: Optional[Dict[str, Any]] = None
    #: B1: the run's own text for memory recall (the cron job / goal text, minus
    #: the overlays the runner prepends to ``task``). None = recall from ``task``.
    recall_seed: Optional[str] = None
    #: 112 F2: the run is money-class (``core.money.classify.is_money_class`` of its
    #: payload), minted by the cron runner / goal dispatcher. The fallback ladder reads
    #: it so a money run never drops to a non-money model. False = not a money run.
    money_class: bool = False

    def __post_init__(self):
        if self.tools is None:
            self.tools = default_session_tools()
        if not self.provider or not self.model:
            self.provider, self.model = _resolve_session_runtime(
                self.provider, self.model
            )
        from agents.task.billed_request import validate_billed_request
        validate_billed_request(self)
def _resolve_chat_runtime(env=None, user_id=None):
    """(provider, model) for a chat with no live session — the ONE chat ladder (112 W6):
    ``core.model_pins.chat_runtime_for`` = the owner's ``/model … default`` (when *user_id*
    is given) > the operator pin > the first keyed provider > openai. The model is filled
    from the provider's registry default. No ``openai/gpt-5`` preference of its own: the
    session path, ``chat_once`` and a recreated session now agree (they diverged before).
    """
    from core.model_pins import chat_runtime_for
    from modules.llm.llm_client_registry import get_default_model
    provider, model, _reasoning, _source = chat_runtime_for(user_id, env)
    provider = provider or "openai"
    if not model:
        model = get_default_model(provider)
    return provider, model


def operator_pin_stamp(provider, model, env=None):
    """The operator pin pair ``{"provider", "model"}`` when a session is created ON it,
    else ``None`` (an explicit per-session model). Stored as ``operator_pin`` in the
    session record so a recreate can tell "it ran on the pin" from "it chose a model"
    (2026-10-14 review B-4). Fail-open to ``None``."""
    try:
        pin_provider, pin_model = _resolve_chat_runtime(env)
        from core.runtime_config import canonicalize_provider as _canon
        if (provider and model and _canon(provider) == _canon(pin_provider)
                and model == pin_model):
            return {"provider": pin_provider, "model": pin_model}
    except Exception:
        logger.debug("operator pin stamp failed", exc_info=True)
    return None


def refused_stored_pin(session_info) -> str:
    """Why a RECREATED billed session may not load its stored model, naming it (``""`` =
    it may). The caller rebuilds it on the default model."""
    info = session_info or {}
    if info.get("billing_limited") is not True:
        return ""
    from agents.task.model_pin_state import stored_pin_refusal
    req, cfg = info.get("request") or {}, info.get("config") or {}
    return stored_pin_refusal(req.get("provider") or cfg.get("provider"),
                              req.get("model") or cfg.get("model"))


def refresh_pinned_model(session_info, env=None):
    """``(provider, model)`` to rebuild a RECREATED session on, or ``None`` = keep the
    stored model (2026-10-14 review B-4).

    A live owner chat lives for days and is rebuilt from its stored model after every
    restart, so a model deploy never reached it. Re-resolve when the stored model WAS
    the operator pin and the pin has moved since:

    * a stamped record (``operator_pin``, written at creation): re-resolve only when the
      stored pair equals the stamp — an explicit per-session model is kept;
    * a record from before the stamp: only an OWNER CHAT (``chat_session_key`` or
      ``creator == "owner"``) whose stored provider is still the pinned provider.

    A credit-dead stored provider is handled separately (``resolve_session_runtime``).
    """
    try:
        info = session_info or {}
        req, cfg = info.get("request") or {}, info.get("config") or {}
        provider = req.get("provider") or cfg.get("provider")
        model = req.get("model") or cfg.get("model")
        if not provider or not model:
            return None
        from core.runtime_config import canonicalize_provider as _canon
        pin_provider, pin_model = _resolve_chat_runtime(env)
        if not pin_model or (_canon(provider) == _canon(pin_provider) and model == pin_model):
            return None
        stamp = info.get("operator_pin")
        if isinstance(stamp, dict):
            if not (stamp.get("model") == model
                    and _canon(stamp.get("provider")) == _canon(provider)):
                return None
        elif "operator_pin" in info:
            return None  # stamped None at creation = an explicit model
        elif not (info.get("chat_session_key") or str(info.get("creator") or "") == "owner"):
            return None
        elif _canon(provider) != _canon(pin_provider):
            return None
        return pin_provider, pin_model
    except Exception:
        logger.debug("pinned-model refresh failed; keeping the stored model", exc_info=True)
        return None


def room_session_source(session_source) -> bool:
    """Is this session being created for a ROOM? (044 C2, fail-CLOSED-to-private.)

    Delegates to the ONE derivation (``core.surfaces.room_policy.is_public_source``)
    rather than re-reading ``chat_type`` here — two derivations of "is this public"
    is exactly how one of them goes stale. Lives here rather than in
    ``task_agent_lite`` because that module is under a shrink-only size ratchet.
    """
    try:
        from core.surfaces.room_policy import is_public_source
        return is_public_source(session_source)
    except Exception as e:  # pragma: no cover - import guard
        import logging
        logging.getLogger(__name__).debug(
            "public-session stamp probe failed (treating as private): %s", e)
        return False


def memory_scope_fields(request: dict) -> dict:
    """025: the three optional scope keys of a dict-shaped request (absent = None)."""
    return {k: request.get(k) for k in ("goal_id", "memory_scope", "memory_regime")}


def bind_memory_scope(session_id: str, request) -> None:
    """025: bind *session_id* to its memory scope (from a ``SessionRequest`` or the
    persisted ``request`` dict on recreate). Flag-off / shared = unbound. Fail-open."""
    try:
        from modules.memory.scope import bind_session_scope
        get = request.get if isinstance(request, dict) else (
            lambda k: getattr(request, k, None))
        bind_session_scope(session_id, get("memory_scope"), get("memory_regime"))
    except Exception as e:  # pragma: no cover - a binding must never break a session
        import logging
        logging.getLogger(__name__).debug("memory scope bind skipped: %s", e)


def build_session_metadata(session_request, *, effective_tool_ids, public_session,
                           credit_billed=False) -> dict:
    """The durable session record ``create_session`` writes and
    ``_recreate_orchestrator`` reads back.

    Two keys carry what the legacy shape could not (044 C1):
    ``effective_tools`` — the toolset the session ACTUALLY ran with. ``request.
    tools`` is only the CALLER's ask; a room's real toolset arrives as the
    ``tool_ids`` override, so recreating from ``request.tools`` brought an evicted
    ROOM session back private-shaped (browser + filesystem, no gate). Its own key,
    because an explicit EMPTY toolset (a locked-down ``GROUP_TURN_TOOLS=""`` room)
    is authoritative and any ``or``-chain fallback would swallow it.
    ``public_session`` — the AUDIENCE. The recreate path cannot re-derive it: the
    chat rebind needs the ``session_chat_registry`` service, which does not exist
    at all when the Singular Chat bus is off.
    """
    from datetime import datetime as _dt
    from core.billing_context import is_billed
    tools_config = (session_request.session_config.get('tools_config', {})
                    if session_request.session_config else {})
    return {
        'task': session_request.task,
        'model': session_request.model,
        'tools': session_request.tools,
        'config': {
            'model': session_request.model,
            'provider': session_request.provider,
            'tools': session_request.tools,
            'max_steps': session_request.max_steps,
            'temperature': session_request.temperature,
            'use_vision': session_request.use_vision,
            'tools_config': tools_config,
        },
        'request': session_request.__dict__,
        'billing_limited': credit_billed or is_billed(),
        'effective_tools': list(effective_tool_ids or []),
        'public_session': bool(public_session),
        'operator_pin': operator_pin_stamp(session_request.provider, session_request.model),
        'created_at': _dt.now().isoformat(),
        'status': 'created',
        'orchestrator_ready': True,
    }
