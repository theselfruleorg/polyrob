"""H6: _observe_error_result re-ran the transform + post tool-call hooks on an error
result with NO try/except. A fail-closed post hook that raises here escaped the whole
multi_act action loop (caught only by the outer 'Critical error' handler), so the
failing action got no result and every remaining queued action was silently skipped —
breaking the tool_call<->result pairing invariant. It must swallow a raising hook here.
"""
import asyncio
import logging

from tools.controller.execution import ExecutionMixin
from tools.controller.types import ActionResult


class _Stub(ExecutionMixin):
    def __init__(self, raise_post):
        self.logger = logging.getLogger("t.exec")
        self._raise_post = raise_post
        self.post_calls = 0

    async def _run_transform_tool_result_hooks(self, at, ap, result, ctx):
        return result

    async def _run_post_tool_call_hooks(self, at, ap, result, ctx):
        self.post_calls += 1
        if self._raise_post:
            raise RuntimeError("closed post hook boom")


def test_raising_post_hook_on_error_result_does_not_escape():
    stub = _Stub(raise_post=True)
    result = ActionResult(error="orig failure", include_in_memory=True)
    # Must NOT raise — a raising hook here would abort the whole batch.
    out = asyncio.run(stub._observe_error_result("act", {}, result, None))
    assert out is result
    assert stub.post_calls == 1


def test_non_raising_hooks_pass_through():
    stub = _Stub(raise_post=False)
    result = ActionResult(error="orig", include_in_memory=True)
    out = asyncio.run(stub._observe_error_result("act", {}, result, None))
    assert out is result
    assert stub.post_calls == 1
