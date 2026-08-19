"""P4 (context-usage audit 2026-08-15) — the registry exposes a token estimate
for the emitted tool-schema list, memoized alongside the schema cache, so the
step loop can stamp it onto the MessageManager gauge without per-step JSON work.
"""
import json

import agents.task.agent.service  # noqa: F401 — avoid import cycle
from tools.controller.registry.service import Registry


def _registry_with_action():
    reg = Registry()

    @reg.action(description="Say hello to the user with a friendly greeting")
    def greet(text: str):  # pragma: no cover - never executed
        return text

    return reg


def test_schema_token_estimate_matches_emitted_bytes():
    reg = _registry_with_action()
    schemas = reg.get_all_actions_for_provider("openai")
    assert schemas  # precondition: something was emitted
    est = reg.get_schema_token_estimate("openai")
    assert est == len(json.dumps(schemas, default=str)) // 4
    assert est > 0


def test_schema_token_estimate_without_prior_schema_call():
    # The getter must self-serve (generate + memoize) when called first.
    reg = _registry_with_action()
    est = reg.get_schema_token_estimate("openai")
    assert est > 0


def test_schema_token_estimate_empty_registry_is_zero():
    reg = Registry()
    assert reg.get_schema_token_estimate("openai") == 0


def test_controller_exposes_schema_token_estimate():
    """The step loop stamps via the Controller, not by reaching into registry."""
    import logging
    from tools.controller.service import Controller

    c = object.__new__(Controller)
    c.logger = logging.getLogger("schema-estimate-test")
    c.registry = _registry_with_action()
    assert c.get_tool_schema_token_estimate("openai") > 0
