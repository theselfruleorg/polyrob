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
    emitted = _registry(_NAMES).get_all_actions_for_provider("openai")
    got = [t["function"]["name"] for t in emitted if isinstance(t, dict)]
    assert got == sorted(_NAMES), "default must be the registration-independent order"


def test_two_registries_with_the_same_ids_emit_identical_bytes(monkeypatch):
    a = _registry(_NAMES).get_all_actions_for_provider("openai")
    b = _registry(list(reversed(_NAMES))).get_all_actions_for_provider("openai")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_stable_order_is_sorted_by_action_name(monkeypatch):
    emitted = _registry(_NAMES).get_all_actions_for_provider("openai")
    got = [t["function"]["name"] for t in emitted if isinstance(t, dict)]
    assert got == sorted(_NAMES)


def test_common_tools_come_first():
    """106 C8: a prefix cache stops at the first difference, so what most rigs hold
    goes first. Anything else follows by name."""
    from tools.controller.registry.tool_order import emit_rank
    names = ["zeta", "agent_avatar", "filesystem_read_file", "done", "alpha", "task_todo_add"]
    assert sorted(names, key=emit_rank) == [
        "done", "task_todo_add", "filesystem_read_file", "agent_avatar", "alpha", "zeta"]


def test_the_rigs_share_the_head_of_their_tools():
    """The order is measured on the rendered rigs, so it must hold on them. A rig that
    has a common tool holds it at the same place as the other rigs."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[5] / "tests" / "golden" / "prompt_surface"
    rigs = {f.name.split(".")[0]: [t["name"] for t in json.loads(f.read_text())["tools"]]
            for f in root.glob("*.tools.json")}
    every = set.intersection(*(set(names) for names in rigs.values()))
    assert len(every) >= 11
    heads = {tuple(names[:len(every)]) for names in rigs.values()}
    assert len(heads) == 1, "the tools every rig holds are not the shared head"

    def shared(a, b):
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n
    assert shared(rigs["cron"], rigs["prod_owner_telegram_dm"]) >= 40
    assert shared(rigs["owner_repl"], rigs["prod_owner_telegram_dm"]) >= 40
