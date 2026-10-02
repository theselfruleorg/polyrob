"""A `defi_data` verb must not hold the event loop while it does network I/O.

⚠️ The defect, measured in production 2026-09-23. The SAFETY monitor — the one
money rail still enabled while trading was paused — produced NOTHING at 18:20,
20:20 and 22:20. All three runs died the same way:

    20:21:07  Received 5 tool_calls from LLM
    20:21:08  Action 1/5: defi_data_portfolio  {'chain': 'robinhood'}
              …no activity for 600 seconds…
    20:31:00  Agent stalled: No activity for 601.3 seconds

There IS a per-action timeout. `tools/controller/execution.py` wraps every call
in ``asyncio.wait_for(self.act(...), timeout=action_timeout)``, and `defi_data`
takes ``TOOL_TIMEOUTS['default']`` = 60 s. **Its message never appeared once in
any of the three failures.** The guard did not fire late; it could not fire at
all.

Because every verb on this tool is declared ``async def`` while its body does
*synchronous* ``httpx`` / ``urllib`` I/O. `asyncio.wait_for`'s expiry is a
callback that needs the event loop to run, and the loop is exactly what the
blocking socket is holding. So the 60-second guard is unenforceable by
construction — and so is every other async timer in the process, which is also
why the Telegram poller logged `get_updates network blip` at 17:06, 20:30 and
22:30.

The framework already does the right thing for a *sync* action:
`tools/controller/registry/service.py` wraps one in ``asyncio.to_thread`` under
the comment "Wrap sync functions to make them async". These verbs declined that
protection by being ``async def`` — async in name only, which is strictly worse
than a plain ``def`` would have been.

## Why the fix is NOT "drop the async keyword"

I told the owner twice that it was. That is wrong, and this file is where the
reason is pinned so nobody tries it:

- ``portfolio`` is awaited directly by non-test callers — `tools/defi/book.py`
  (twice) and `webview/pages.py` — and roughly thirty unit tests await these
  methods. Removing ``async`` makes every one of those ``await`` a TypeError.
- ``portfolio`` and ``reconcile`` contain a real ``await`` of a sibling method.

So the signature must STAY a coroutine. The body moves to a sync helper and the
public verb becomes ``await asyncio.to_thread(self._x_sync, ...)``: invisible to
every caller, and it frees the loop so the existing 60 s guard starts working.
Test four below pins the signature half of that contract.
"""
import asyncio
import time
import types

import pytest

from tools.defi.data_tool import DefiDataTool, TokenRefParams
from tools.defi.providers.base import HolderReport, HolderRow, PriceInfo

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Long enough that a blocking body cannot be mistaken for a fast one, short
# enough that the suite does not pay for it: only the AWAITING side waits, and
# after the fix every test here returns in well under a second.
_SLOW_SEC = 1.5


def _ident(chain, addr):
    return types.SimpleNamespace(symbol="TKN", name="Token", decimals=18,
                                 verified=True, metadata_changed=False)


def _report():
    return HolderReport(
        available=True, holder_count=10, total_supply=1_000.0,
        top_holders=[HolderRow("0xwhale", percent=0.2, is_contract=False, is_locked=False)],
        lp_holders=[], lp_holder_count=0, lp_total_supply=0.0,
        creator_address=None, creator_percent=None,
        owner_address=None, owner_percent=None,
        honeypot_with_same_creator=False,
    )


def _slow_price(chain, addr):
    """A provider that is slow but perfectly legal — the production shape.

    GeckoTerminal's own ceiling is 12 s and every call in this path is bounded.
    Nothing here misbehaves; blocking the loop is enough on its own.
    """
    time.sleep(_SLOW_SEC)
    return PriceInfo(price_usd=1.0, liquidity_usd=1_000.0, pool_count=1, confidence="high")


def _slow_holders(chain, addr):
    time.sleep(_SLOW_SEC)
    return _report()


def _fast_price(chain, addr):
    return PriceInfo(price_usd=2.5, liquidity_usd=9_000.0, pool_count=2, confidence="high")


# --- the production symptom: the guard that never fired --------------------- #

@pytest.mark.asyncio
async def test_wait_for_can_cancel_a_slow_token_info():
    """This is the 60-second guard in `execution.py`, in miniature.

    Before the fix the verb runs to completion inside the first step of the task
    and `wait_for` hands back a RESULT — no TimeoutError, after the full blocking
    wait. That is precisely how a 60 s bound let a 600 s stall through.
    """
    tool = DefiDataTool(identity_fn=_ident, price_fn=_slow_price,
                        holders_fn=lambda c, a: _report())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            tool.token_info(TokenRefParams(chain="base", address=TOKEN)), timeout=0.2)


@pytest.mark.asyncio
async def test_wait_for_can_cancel_a_slow_price():
    tool = DefiDataTool(identity_fn=_ident, price_fn=_slow_price,
                        holders_fn=lambda c, a: _report())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            tool.price(TokenRefParams(chain="base", address=TOKEN)), timeout=0.2)


@pytest.mark.asyncio
async def test_wait_for_can_cancel_a_slow_token_holders():
    tool = DefiDataTool(identity_fn=_ident, price_fn=_fast_price,
                        holders_fn=_slow_holders)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            tool.token_holders(TokenRefParams(chain="base", address=TOKEN)), timeout=0.2)


@pytest.mark.asyncio
async def test_the_loop_keeps_running_while_a_verb_does_its_io():
    """The other half of the outage: a blocked loop starves everything else.

    The Telegram poller's `get_updates network blip` warnings clustered at
    exactly the times a `defi_data` batch was in flight. A heartbeat proves the
    loop is alive rather than merely that the verb returned.
    """
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        tool = DefiDataTool(identity_fn=_ident, price_fn=_slow_price,
                            holders_fn=lambda c, a: _report())
        await tool.token_info(TokenRefParams(chain="base", address=TOKEN))
    finally:
        beat.cancel()
    assert ticks >= 10, (
        f"the event loop advanced only {ticks} times during a {_SLOW_SEC}s verb — "
        "the blocking body is still running on the loop thread")


# --- the contract that makes "drop the async keyword" wrong ------------------ #

#: Every verb on the tool. An AST walk on 2026-09-24 found NINETEEN `async def`
#: methods here and SEVENTEEN with no `await` at all — the four originally named
#: were not special, and any one of them in a batch can hold the loop for its
#: whole read. All of them are threaded now; this list is the ratchet.
_THREADED_VERBS = [
    "token_resolve", "token_info", "scan", "ohlcv", "token_holders", "swap_quote",
    "price", "portfolio", "new_pools", "trending", "nft_holdings", "nft_info",
    "lp_positions", "lp_pool_info", "lp_quote", "contract_read", "pool_metrics",
]


@pytest.mark.parametrize("name", _THREADED_VERBS + ["reconcile"])
def test_the_public_verb_is_still_a_coroutine_function(name):
    """`tools/defi/book.py`, `webview/pages.py` and ~30 tests `await` these.

    Threading the body must not change the signature. If this fails, the fix was
    applied by deleting `async` and three non-test call sites now raise
    TypeError on `await`.
    """
    assert asyncio.iscoroutinefunction(getattr(DefiDataTool, name)), (
        f"{name} must stay a coroutine function — callers await it directly")


@pytest.mark.parametrize("name", [f"_{v}_sync" for v in _THREADED_VERBS]
                         + ["_reconcile_impl_sync", "_solana_portfolio_sync"])
def test_the_body_lives_in_a_plain_sync_helper(name):
    """And the blocking work is a plain `def`, so `to_thread` can take it."""
    fn = getattr(DefiDataTool, name, None)
    assert fn is not None, f"{name} is missing — the body was not moved off the loop"
    assert not asyncio.iscoroutinefunction(fn), f"{name} must be a plain def"


def test_no_verb_is_left_async_in_name_only():
    """The ratchet. An `async def` whose body contains no `await` is doing
    synchronous work on the event loop — the defect this whole file is about.
    A new verb written that way fails here instead of in production."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(inspect.getmodule(DefiDataTool)))
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for fn in node.body:
            if isinstance(fn, ast.AsyncFunctionDef) and not any(
                    isinstance(n, ast.Await) for n in ast.walk(fn)):
                offenders.append(f"{node.name}.{fn.name} (line {fn.lineno})")
    assert offenders == [], (
        "async def with no await — move the body to a sync helper and call it "
        f"via asyncio.to_thread: {offenders}")


# --- the fix must not change the answer ------------------------------------- #

@pytest.mark.asyncio
async def test_threading_does_not_change_what_the_verb_reports():
    tool = DefiDataTool(identity_fn=_ident, price_fn=_fast_price,
                        holders_fn=lambda c, a: _report())
    res = await tool.token_info(TokenRefParams(chain="base", address=TOKEN))
    out = (res.extracted_content or "") + (res.error or "")
    assert TOKEN in out and "TKN" in out
    assert "2.5" in out or "2.50" in out


@pytest.mark.asyncio
async def test_an_error_inside_the_thread_still_reaches_the_caller():
    """A refusal or exception must not be swallowed by the worker thread."""
    def _boom(chain, addr):
        raise RuntimeError("provider exploded")

    tool = DefiDataTool(identity_fn=_ident, price_fn=_fast_price, holders_fn=_boom)
    res = await tool.token_holders(TokenRefParams(chain="base", address=TOKEN))
    assert "provider exploded" in ((res.error or "") + (res.extracted_content or ""))
