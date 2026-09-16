"""Module-level helpers shared by ``TaskAgent`` and its mixins (S8 split, 2026-08-29).

Moved verbatim out of ``agents/task_agent_lite.py`` so the chat / delivery / lifecycle
mixins can import them without a cycle; ``task_agent_lite`` re-exports every name, so
``from agents.task_agent_lite import SessionRequest`` (and the test seams
``_spawn_detached`` / ``_DETACHED_TASKS`` / ``_SELF_WAKE_TASKS``) keep working on the SAME
objects.
"""
from agents.task.tool_defaults import default_session_tools
from core.optional_extras import missing_extra_hint
from dataclasses import dataclass
from typing import Optional, Dict, Any, Union, List
import asyncio


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
#: Mirrors ``core/autonomy_runtime.py::_BACKGROUND_TASKS``. Self-cleans via
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
    t = asyncio.create_task(coro)
    _DETACHED_TASKS.add(t)
    t.add_done_callback(_DETACHED_TASKS.discard)
    return t
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
    max_steps: int = 50
    temperature: float = 0.0
    use_vision: bool = True
    session_config: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.tools is None:
            self.tools = default_session_tools()
        if not self.provider or not self.model:
            self.provider, self.model = _resolve_session_runtime(
                self.provider, self.model
            )
def _resolve_chat_runtime(env=None):
    """Resolve (provider, model) for chat_once via the shared core resolver (Seam 2).

    Precedence: CHAT_PROVIDER/CHAT_MODEL or DEFAULT_PROVIDER/DEFAULT_MODEL pin >
    the historical openai/gpt-5 default (used only if an OpenAI key is present) >
    first keyed provider (canonical order) > openai/gpt-5 last resort. The model is
    always filled to match the resolved provider, so a first-keyed/anthropic-pinned
    provider never inherits SessionRequest's gpt-5 default.
    """
    import os as _os
    from core.runtime_config import resolve_runtime_config
    from modules.llm.llm_client_registry import get_default_model

    env = _os.environ if env is None else env
    pinned_provider = env.get("CHAT_PROVIDER") or env.get("DEFAULT_PROVIDER")
    pinned_model = env.get("CHAT_MODEL") or env.get("DEFAULT_MODEL")
    provider, model = resolve_runtime_config(
        None,
        None,
        env=env,
        pinned_provider=pinned_provider,
        pinned_model=pinned_model,
        cli_store_default=("openai", "gpt-5"),
        last_resort=("openai", "gpt-5"),
    )
    if not model:
        model = get_default_model(provider)
    return provider, model


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


def build_session_metadata(session_request, *, effective_tool_ids, public_session) -> dict:
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
        'effective_tools': list(effective_tool_ids or []),
        'public_session': bool(public_session),
        'created_at': _dt.now().isoformat(),
        'status': 'created',
        'orchestrator_ready': True,
    }
