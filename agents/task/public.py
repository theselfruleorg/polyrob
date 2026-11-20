"""The public face of the agent core for the tiers above ``agents`` (proposal 125 S29).

Code outside ``agents/`` (tools, cron, the composition root, the seats) reads an
agent-core name from HERE, never from ``agents.task.agent.core.*`` directly:
``tests/test_agent_core_facade_ratchet.py`` holds a shrink-only list of the
direct edges that remain. The agent core can then change its module layout
without a fan-out over every tier.

Resolution is LAZY and happens on every attribute access (PEP 562
``__getattr__``), as in :mod:`polyrob.sdk`: importing this module loads no
agent-core module, and a test that monkeypatches the HOST attribute is seen by
a caller that imports the name at call time.

Add a name: one ``NAME: "module:attribute"`` row in ``_EXPORTS``.
"""
import importlib
from typing import Any, Dict

#: public name -> "agents.task.agent.core.<module>:attribute"
_EXPORTS: Dict[str, str] = {
    # --- run records and the lane digest (cron, insights) ---------------------
    "record_run": "agents.task.agent.core.episodic_digest:record_run",
    "render_previous_runs": "agents.task.agent.core.episodic_digest:render_previous_runs",
    "did_something": "agents.task.agent.core.episodic_digest:did_something",
    "run_record_facts": "agents.task.agent.core.episodic_digest:run_record_facts",
    "lane_outcome_rates": "agents.task.agent.core.episodic_digest:lane_outcome_rates",
    "render_lane_rates": "agents.task.agent.core.episodic_digest:render_lane_rates",
    "schedule_review_after_run":
        "agents.task.agent.core.background_review:schedule_review_after_run",
    # --- background loops (the composition root) ------------------------------
    "build_curator_ticker": "agents.task.agent.core.curator:build_curator_ticker",
    "RETENTION_INTERVAL_SEC": "agents.task.agent.core.retention:RETENTION_INTERVAL_SEC",
    "RetentionSweeper": "agents.task.agent.core.retention:RetentionSweeper",
    # --- run budget, health and self-wake (status, seats, tools) --------------
    "RUN_BUDGET_MARKER": "agents.task.agent.core.run_budget:RUN_BUDGET_MARKER",
    "run_spend_vs_cap": "agents.task.agent.core.run_budget:run_spend_vs_cap",
    "model_routing_line": "agents.task.agent.core.live_health:model_routing_line",
    "effective_self_wake_enabled": "agents.task.agent.core.self_wake:effective_self_wake_enabled",
    # --- memory reads (controller actions) ------------------------------------
    "memory_read_error": "agents.task.agent.core.memory_prefetch:memory_read_error",
    "recall_reader": "agents.task.agent.core.memory_prefetch:recall_reader",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'agents.task.public' has no attribute {name!r}")
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def __dir__():
    return __all__
