"""122 W1: ``multi_act`` binds each action's ``why`` for the action in flight — the hooks and
the action see it, the next action does not inherit it, and the batch leaves nothing bound."""
import asyncio
import logging
from types import SimpleNamespace
from typing import Optional

from pydantic import BaseModel

from agents.task.loop_types.controller_types import ActionResult
from agents.task.loop_types.execution_context import ActionExecutionContext
from core.act_reason import current_act_why
from tools.controller.execution import ExecutionMixin


class _Why(BaseModel):
    why: Optional[str] = None


class _Action(BaseModel):
    send_money: Optional[_Why] = None

    def get_index(self):
        return None


class _Ctrl(ExecutionMixin):
    def __init__(self):
        self.logger = logging.getLogger("t.why")
        self.registry = SimpleNamespace(get_action=lambda name: None)
        self.session_id = "s1"
        self.hook_saw, self.act_saw = [], []

    async def _run_pre_tool_call_hooks(self, name, params, ctx):
        self.hook_saw.append(current_act_why())
        return None

    async def act(self, action, execution_context=None, **kw):
        self.act_saw.append(current_act_why())
        return ActionResult(extracted_content="ok")

    def __getattr__(self, name):
        async def _anull(*a, **k):
            return a[2] if len(a) > 2 and isinstance(a[2], ActionResult) else None
        if name.startswith("_run_") or name.startswith("_observe"):
            return _anull
        return lambda *a, **k: None


def test_each_action_carries_its_own_reason_and_the_batch_clears_it():
    c = _Ctrl()
    ctx = ActionExecutionContext(role="orchestrator", session_id="s1", user_id="u")
    asyncio.run(c.multi_act([_Action(send_money=_Why(why="take profit")),
                             _Action(send_money=_Why())], execution_context=ctx))
    assert c.hook_saw == ["take profit", ""]
    assert c.act_saw == ["take profit", ""]
    assert current_act_why() == ""
