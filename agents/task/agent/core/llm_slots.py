"""The per-session LLM client slots that several paths share: closing an isolated
client this session built (never a shared one), and the lazily built judge model.

Kept apart from ``model_swap`` (which re-exports both) so the owner-verdict and review
paths reach them with no import of the swap machinery and its construction helpers.
"""
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def close_own_llm(llm: Any, container: Any = None, *, may_be_shared: bool = False) -> None:
    """Close a model this session built on an isolated client (aux, judge, swap).

    ⚠️ Never the shared per-provider client: closing it stops every session that
    uses the provider. Each caller holds only isolated models, and the check against
    the manager's registered clients is the second lock on that door.

    ``may_be_shared=True`` is the session's MAIN slot, which holds the shared client
    on most sessions: the refusal is then the expected path and logs at DEBUG. The aux,
    judge and swap slots keep the WARNING — a shared client there is a defect.
    """
    if llm is None:
        return
    client = getattr(llm, "_client", llm)
    try:
        manager = container.get_service("llm") if container is not None else None
        shared = (getattr(manager, "clients", None) or {}).values()
    except Exception:
        shared = ()
    if any(client is registered for registered in shared):
        (logger.debug if may_be_shared else logger.warning)(
            "refused to close the shared LLM client %s", type(client).__name__)
        return
    close = getattr(client, "cleanup", None) or getattr(client, "aclose", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        logger.debug("closing an isolated LLM client failed: %s", e)


async def cached_judge_llm(agent: Any) -> Any:
    """The agent's judge model, built once and cached on ``_judge_llm``; None when no aux
    model resolves. Two tasks that build it at once keep one and close the other, and a
    model built after the session closed its models is closed at once (nothing else
    would close it). Raises what the provisioning raises."""
    llm = getattr(agent, "_judge_llm", None)
    if llm is not None:
        return llm
    built = await agent._provision_aux_llm_async("judge")
    if built is None:
        return None
    container = getattr(agent, "container", None)
    if getattr(agent, "_llms_closed", False):
        await close_own_llm(built, container)
        return None
    current = getattr(agent, "_judge_llm", None)
    if current is not None:
        await close_own_llm(built, container)
        return current
    agent._judge_llm = built  # closed once at session cleanup
    return built
