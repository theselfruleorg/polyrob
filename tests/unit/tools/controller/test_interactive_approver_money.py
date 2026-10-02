"""The REPL approval ladder never widens a money approval.

``[s]ession``/``[a]lways`` are keyed by the action NAME only, so one ``s`` on a
money verb used to cover every later call at any amount. For a money action the
ladder offers only o/d/n, and a typed s/a approves THIS call only.
"""
import pytest

from tools.controller import approval_interactive
from tools.controller.approval_interactive import (
    MONEY_ONE_AT_A_TIME, InteractiveCLIApprover)

MONEY = "defi_trade_swap"
PLAIN = "git_push"


@pytest.fixture(autouse=True)
def _free_stdin():
    """An earlier test's timed-out reader thread may still hold the one-stdin
    flag; these tests model a free prompt."""
    approval_interactive._stdin_in_flight.clear()
    yield
    approval_interactive._stdin_in_flight.clear()


def _recording(answer):
    prompts = []

    def _fn(prompt):
        prompts.append(prompt)
        return answer
    return _fn, prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["s", "a"])
async def test_money_s_or_a_approves_once_and_asks_again(answer):
    fn, prompts = _recording(answer)
    prov = InteractiveCLIApprover(input_fn=fn)
    assert await prov.request(MONEY, {"amount_in": 1}, None) is True
    assert await prov.request(MONEY, {"amount_in": 5000}, None) is True
    # The second, bigger call PROMPTED again: nothing was remembered.
    assert len(prompts) == 2
    assert MONEY not in prov._session_approved


@pytest.mark.asyncio
async def test_money_prompt_does_not_offer_session_or_always():
    fn, prompts = _recording("d")
    prov = InteractiveCLIApprover(input_fn=fn)
    assert await prov.request(MONEY, {"amount_in": 1}, None) is False
    assert MONEY_ONE_AT_A_TIME in prompts[0]
    assert "[s]ession" not in prompts[0] and "[a]lways" not in prompts[0]
    assert "[o]nce" in prompts[0]


@pytest.mark.asyncio
async def test_money_is_never_short_circuited_even_if_remembered():
    fn, prompts = _recording("d")
    prov = InteractiveCLIApprover(input_fn=fn)
    prov._session_approved.add(MONEY)  # a stale entry must not approve money
    assert await prov.request(MONEY, {"amount_in": 1}, None) is False
    assert len(prompts) == 1


@pytest.mark.asyncio
async def test_a_non_money_action_still_offers_and_keeps_session():
    fn, prompts = _recording("s")
    prov = InteractiveCLIApprover(input_fn=fn)
    assert await prov.request(PLAIN, {}, None) is True
    assert "[s]ession" in prompts[0]
    assert await prov.request(PLAIN, {}, None) is True
    assert len(prompts) == 1  # remembered for the session


def test_the_money_reprompt_offers_only_o_d_n():
    assert "o/d/n" in InteractiveCLIApprover._reprompt(MONEY)
    assert "o/s/a/d/n" in InteractiveCLIApprover._reprompt(PLAIN)
