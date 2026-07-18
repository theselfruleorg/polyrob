"""§6.2 fail-closed metering gate for money-enabled autonomous runs.

Independent of the (now-removed, Task 9) autonomy budget cap: without a
database_manager, spend tracking (``usage_records``) is empty, so a
money-moving run on a live mainnet wallet would fly blind — no record of what
it spent, ever. A run whose toolset can move money must therefore refuse to
START unmetered — a clear, recorded error instead of a silent unmetered spend
loop. Non-money runs are unaffected (they degrade to unmetered exactly as
before).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Toolsets that can move money (spend or receivables). Env-independent constant —
# the gate is a safety invariant, not a tuning knob. WS-2: derived from the ONE
# per-tool capability table (core/tool_capabilities.py) — classify a new money tool
# there, not here. Parity-pinned by tests/unit/core/test_tool_capabilities.py.
from core.tool_capabilities import ids_with as _ids_with

MONEY_TOOLS = _ids_with("money")


def metering_available(task_agent: Any) -> bool:
    try:
        container = getattr(task_agent, "container", None)
        if container is None or not hasattr(container, "get_service"):
            return False
        return container.get_service("database_manager") is not None
    except Exception:
        return False


def unmetered_money_gate(task_agent: Any, tools: Optional[Iterable[str]]) -> Optional[str]:
    """Return the refusal error when a money-enabled run would start unmetered;
    None when the run may proceed. Never raises."""
    try:
        money = MONEY_TOOLS.intersection(str(t) for t in (tools or []))
        if not money:
            return None
        if metering_available(task_agent):
            return None
        return ("refused: money-enabled autonomous run (tools: "
                f"{', '.join(sorted(money))}) without metering — no "
                "database_manager, so spend would be UNMETERED and untracked "
                "(§6.2 fail-closed)")
    except Exception:
        logger.debug("metering gate check failed (fail-open)", exc_info=True)
        return None
