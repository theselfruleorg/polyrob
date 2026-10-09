"""AGT-18: multi_act ran the pre-hooks against the FIRST key of
``model_dump(exclude_unset=True)`` (an explicit null counts), while ``act()`` runs
the first NON-None key. An action object that names two actions — the first
null — would be judged as one verb and run as the other. The hooks now name the
action the way act() does, and an object naming more than one action runs
nothing."""
import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

from pydantic import BaseModel

from tools.controller.execution import ExecutionMixin
from tools.controller.execution_context import ActionExecutionContext
from tools.controller.types import ActionResult


class _P(BaseModel):
    x: int = 0


class _Action(BaseModel):
    read_file: Optional[_P] = None
    send_money: Optional[_P] = None

    def get_index(self):
        return None


class _Ctrl(ExecutionMixin):
    def __init__(self):
        self.logger = logging.getLogger("t.agt18")
        self.registry = SimpleNamespace(get_action=lambda name: None)
        self.session_id = "s1"
        self.hooked, self.ran = [], []

    async def _run_pre_tool_call_hooks(self, name, params, ctx):
        self.hooked.append(name)
        return "denied" if name == "send_money" else None

    async def act(self, action, execution_context=None, **kw):
        data = {k: v for k, v in action.model_dump(exclude_unset=True).items() if v is not None}
        self.ran.append(next(iter(data)))
        return ActionResult(extracted_content="ok")

    def __getattr__(self, name):  # telemetry / hook stubs the loop touches
        async def _anull(*a, **k):
            return a[2] if len(a) > 2 and isinstance(a[2], ActionResult) else None
        if name.startswith("_run_") or name.startswith("_observe"):
            return _anull
        return lambda *a, **k: None


def _run(action):
    c = _Ctrl()
    ctx = ActionExecutionContext(role="orchestrator", session_id="s1", user_id="u")
    out = asyncio.run(c.multi_act([action], execution_context=ctx))
    return c, out


def test_explicit_null_first_key_is_judged_as_the_verb_that_runs():
    c, out = _run(_Action(read_file=None, send_money=_P(x=1)))
    assert c.hooked == ["send_money"]          # the hook saw the real verb
    assert c.ran == []                          # and its denial held
    assert out[0].error and "blocked" in out[0].error


def test_two_named_actions_run_nothing():
    c, out = _run(_Action(read_file=_P(), send_money=_P(x=1)))
    assert c.hooked == [] and c.ran == []
    assert out[0].error and "exactly one action" in out[0].error


def test_one_action_still_runs():
    c, out = _run(_Action(read_file=_P()))
    assert c.hooked == ["read_file"] and c.ran == ["read_file"]
