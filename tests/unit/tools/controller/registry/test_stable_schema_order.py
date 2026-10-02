"""057 WS-A: the emitted tool-schema bytes must not depend on registration order.

The schema list rides the prompt's cached prefix. Its order was dict INSERTION
order — the order tools happened to register in — so a cron session and a chat
session holding the SAME tools emitted a different prefix and paid a cold cache
at 50x the cached input price.
"""
import json

import pytest
from pydantic import BaseModel

from tools.controller.registry.service import Registry


class _P(BaseModel):
    x: int = 0


async def _fn(params, execution_context=None):
    from agents.task.agent.views import ActionResult
    return ActionResult(extracted_content="ok")


def _registry(names):
    reg = Registry()
    for n in names:
        reg.wrap_function(n, _fn, f"does {n}", tool="t", param_model=_P)
    return reg


_NAMES = ["zeta_action", "alpha_action", "mid_action", "beta_action"]


def test_default_is_sorted_order(monkeypatch):
    """2026-09-22 harness/cache review F1: the default is the stable order."""
    monkeypatch.delenv("TOOL_SCHEMA_STABLE_ORDER", raising=False)
    emitted = _registry(_NAMES).get_all_actions_for_provider("openai")
    got = [t["function"]["name"] for t in emitted if isinstance(t, dict)]
    assert got == sorted(_NAMES), "default must be the registration-independent order"


def test_false_restores_registration_order(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMA_STABLE_ORDER", "false")
    emitted = _registry(_NAMES).get_all_actions_for_provider("openai")
    got = [t["function"]["name"] for t in emitted if isinstance(t, dict)]
    assert got == _NAMES, "`false` is the pre-057 escape"


def test_two_registries_with_the_same_ids_emit_identical_bytes(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMA_STABLE_ORDER", "true")
    a = _registry(_NAMES).get_all_actions_for_provider("openai")
    b = _registry(list(reversed(_NAMES))).get_all_actions_for_provider("openai")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_stable_order_is_sorted_by_action_name(monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMA_STABLE_ORDER", "true")
    emitted = _registry(_NAMES).get_all_actions_for_provider("openai")
    got = [t["function"]["name"] for t in emitted if isinstance(t, dict)]
    assert got == sorted(_NAMES)
