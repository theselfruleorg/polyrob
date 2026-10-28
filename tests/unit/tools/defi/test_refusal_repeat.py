"""Review 2026-10-13 C-09: a refused money call sent again with the SAME
arguments says so plainly; a changed argument does not get the note."""
from types import SimpleNamespace

import pytest

from tests.unit.tools.defi.test_trade_t4 import ROUTER, USDC, _tool
from tools.defi import refusal_repeat
from tools.defi.trade_tool import ApproveParams


@pytest.fixture(autouse=True)
def _clean():
    refusal_repeat.reset_for_tests()
    yield
    refusal_repeat.reset_for_tests()


def _ctx(step, session="run-c09"):
    return SimpleNamespace(session_id=session, user_id="owner", trace_id="t",
                           metadata={"step": step})


async def _approve(tool, max_spend, step, session="run-c09"):
    res = await tool.approve_token(ApproveParams(chain="base",
        token=USDC, spender=ROUTER, amount=1.0, max_spend_usd=max_spend,
        dry_run=True), execution_context=_ctx(step, session))
    return res.extracted_content or res.error or ""


@pytest.mark.asyncio
async def test_identical_args_after_a_refusal_say_so():
    tool, _ = _tool(allow=False, price=1.0)
    first = await _approve(tool, 2.0, step=11)
    assert "NOT SENT" in first and "SAME ARGUMENTS" not in first
    second = await _approve(tool, 2.0, step=12)
    assert "NOT SENT" in second
    assert "SAME ARGUMENTS as a call the guard already refused" in second


@pytest.mark.asyncio
async def test_a_changed_argument_or_another_run_gets_no_note():
    tool, _ = _tool(allow=False, price=1.0)
    await _approve(tool, 2.0, step=1)
    assert "SAME ARGUMENTS" not in await _approve(tool, 3.0, step=2)
    assert "SAME ARGUMENTS" not in await _approve(tool, 2.0, step=3, session="other-run")


@pytest.mark.asyncio
async def test_an_allowed_call_is_never_noted():
    tool, _ = _tool(allow=True, price=1.0)
    await _approve(tool, 2.0, step=1)
    assert "SAME ARGUMENTS" not in await _approve(tool, 2.0, step=2)


def test_a_different_refusal_reason_gets_no_note():
    """A retry whose refusal changed (a transient fault, a cap just raised) is not
    an 'identical refusal'."""
    ctx = _ctx(1)
    intent = SimpleNamespace(a=1)
    assert refusal_repeat.note_refusal(ctx, "swap", intent, "rpc timeout") is False
    assert refusal_repeat.note_refusal(ctx, "swap", intent, "per-transaction cap") is False
    assert refusal_repeat.note_refusal(ctx, "swap", intent, "per-transaction cap") is True
