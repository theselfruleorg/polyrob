"""Task 9 / G-2 — the ``owner_queue`` ApprovalProvider: a durable, remote-capable
owner approval queue built on the SAME asks store the goal-board budget gate uses.

Covers: approve-before-timeout, reject, timeout (ask remains + no dangling poll),
the post-timeout one-shot grant (single redemption), notification dedup on retry,
and the forged/leaf-turn defense-in-depth guard.
"""
import asyncio

import pytest

from agents.task.goals.board import ASK_OPEN, GoalBoard
from tools.controller.approval_queue import OwnerQueueApprover, compute_request_hash
from tools.controller.execution_context import ActionExecutionContext


def _ctx(user_id="u1", session_id="s1", **kwargs):
    return ActionExecutionContext(session_id=session_id, user_id=user_id,
                                  role=kwargs.pop("role", "orchestrator"), **kwargs)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.fixture
def provider(board):
    # A fast poll interval keeps these tests quick without weakening the assertions.
    return OwnerQueueApprover(board=board, poll_interval=0.02, container=None)


# --- compute_request_hash (pure) --------------------------------------------------

def test_hash_stable_for_identical_params():
    h1 = compute_request_hash("x402_request", {"amount_usd": 5, "purpose": "x"}, "u1")
    h2 = compute_request_hash("x402_request", {"purpose": "x", "amount_usd": 5}, "u1")
    assert h1 == h2  # key order must not matter


def test_hash_differs_for_different_params():
    h1 = compute_request_hash("x402_request", {"amount_usd": 5}, "u1")
    h2 = compute_request_hash("x402_request", {"amount_usd": 500}, "u1")
    assert h1 != h2


def test_hash_differs_for_different_tenant():
    h1 = compute_request_hash("x402_request", {"amount_usd": 5}, "u1")
    h2 = compute_request_hash("x402_request", {"amount_usd": 5}, "u2")
    assert h1 != h2


# --- request() lifecycle -----------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_before_timeout_resolves_true(provider, board):
    ctx = _ctx()
    task = asyncio.create_task(provider.request("x402_request", {"amount_usd": 5}, ctx))
    await asyncio.sleep(0.06)  # let request() create the ask + start polling
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    ok, _ = board.decide_ask(asks[0].id, user_id="u1", approved=True)
    assert ok is True
    result = await asyncio.wait_for(task, timeout=2)
    assert result is True


@pytest.mark.asyncio
async def test_reject_before_timeout_resolves_false(provider, board):
    ctx = _ctx()
    task = asyncio.create_task(provider.request("x402_request", {"amount_usd": 5}, ctx))
    await asyncio.sleep(0.06)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    board.decide_ask(asks[0].id, user_id="u1", approved=False)
    result = await asyncio.wait_for(task, timeout=2)
    assert result is False


@pytest.mark.asyncio
async def test_timeout_with_no_decision_denies_and_ask_remains_visible(provider, board):
    ctx = _ctx()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            provider.request("x402_request", {"amount_usd": 5}, ctx), timeout=0.12)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1  # left visible for the owner, never silently dropped


@pytest.mark.asyncio
async def test_cancellation_runs_finally_no_dangling_poll(provider, board):
    """UP-04 cancellation-safety contract: on the outer wait_for's timeout, the
    `finally` in the poll loop must run — no dangling poll survives the coroutine."""
    ctx = _ctx()
    assert provider._active_polls == 0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            provider.request("x402_request", {"amount_usd": 5}, ctx), timeout=0.12)
    assert provider._active_polls == 0


@pytest.mark.asyncio
async def test_post_timeout_approval_is_a_one_shot_grant(provider, board):
    ctx = _ctx()
    params = {"amount_usd": 7, "purpose": "consulting"}

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    ok, _ = board.decide_ask(asks[0].id, user_id="u1", approved=True)
    assert ok is True

    # The NEXT identical request consumes the one-shot grant -> True, immediately,
    # WITHOUT re-queuing (no new open ask spawned).
    result = await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=1)
    assert result is True
    assert board.asks(user_id="u1", status=ASK_OPEN) == []

    # A SECOND identical retry does NOT get a free grant (single redemption) — it
    # queues again like a fresh request and times out with nobody there to decide.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)


@pytest.mark.asyncio
async def test_expired_grant_is_not_redeemable(provider, board, monkeypatch):
    ctx = _ctx()
    params = {"amount_usd": 9}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    board.decide_ask(asks[0].id, user_id="u1", approved=True)
    # Shrink the TTL to 0 hours so the just-recorded grant reads as already-expired.
    monkeypatch.setattr(OwnerQueueApprover, "_grant_ttl_hours", staticmethod(lambda: 0.0))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)


@pytest.mark.asyncio
async def test_retry_against_open_ask_reuses_it_and_does_not_renotify(provider, board, monkeypatch):
    calls = []

    async def _fake_notify(container, user_id, text):
        calls.append((user_id, text))

    monkeypatch.setattr("tools.controller.approval_queue._push_owner_notification", _fake_notify)
    ctx = _ctx()
    params = {"amount_usd": 3}

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)
    assert len(calls) == 1
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1

    # A second call for the SAME (tool, params, tenant) reuses the SAME open ask —
    # no second ask, no second notification.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)
    assert len(calls) == 1
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1


# --- defense in depth: forged/leaf turns never queue -------------------------------

@pytest.mark.asyncio
async def test_leaf_turn_denied_without_creating_an_ask(provider, board):
    ctx = _ctx(role="leaf", is_sub_agent=True)
    result = await provider.request("x402_request", {"amount_usd": 5}, ctx)
    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


@pytest.mark.asyncio
async def test_forged_turn_kind_denied_without_creating_an_ask(provider, board):
    ctx = _ctx(metadata={"turn_kind": "self_wake"})
    result = await provider.request("x402_request", {"amount_usd": 5}, ctx)
    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


# --- H4: one approval == exactly one execution (in-band consumes the grant) --------

@pytest.mark.asyncio
async def test_in_band_approval_consumes_the_one_shot_grant(provider, board):
    """H4 (direct): when the owner approves WHILE the requester is still polling,
    the poll loop must CONSUME the one-shot grant before returning True. After that,
    the ``grant_consumed`` CAS a later redeemer would use must already lose."""
    ctx = _ctx()
    params = {"amount_usd": 12, "purpose": "consulting"}

    task = asyncio.create_task(provider.request("x402_request", params, ctx))
    await asyncio.sleep(0.06)  # let the ask get created + polling start
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    ask_id = asks[0].id
    ok, _ = board.decide_ask(ask_id, user_id="u1", approved=True)
    assert ok is True
    result = await asyncio.wait_for(task, timeout=2)
    assert result is True

    # The grant was consumed IN-BAND -> a direct CAS now loses (already true).
    assert board.consume_ask_grant(ask_id) is False


@pytest.mark.asyncio
async def test_in_band_approval_is_not_redeemable_as_a_second_grant(provider, board):
    """H4 (behavioral): after an in-band approval, a byte-identical repeat within
    the grant TTL must NOT silently return True off a leftover grant — it queues a
    fresh ask and blocks (nobody decides) -> times out. Before the fix this repeat
    consumed the unconsumed grant and returned True immediately (a SECOND execution
    with zero owner interaction)."""
    ctx = _ctx()
    params = {"amount_usd": 50, "purpose": "consulting"}

    task = asyncio.create_task(provider.request("x402_request", params, ctx))
    await asyncio.sleep(0.06)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    board.decide_ask(asks[0].id, user_id="u1", approved=True)
    assert await asyncio.wait_for(task, timeout=2) is True

    # Byte-identical repeat -> must NOT redeem a leftover grant.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.request("x402_request", params, ctx), timeout=0.12)
    # It queued a brand-new OPEN ask instead of silently succeeding.
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1


# --- MH1: forged/autonomous probe fails CLOSED in owner_queue -----------------------

@pytest.mark.asyncio
async def test_forged_probe_exception_denies_fail_closed(provider, board, monkeypatch):
    """MH1: if the ``_is_forged_or_autonomous_turn`` probe RAISES, owner_queue must
    DENY (fail-closed) — mirroring the correspondent-taint probe beside it — not
    fail-open into creating a durable ask and polling."""
    def _boom(*args, **kwargs):
        raise RuntimeError("forged probe exploded")

    monkeypatch.setattr(
        "tools.controller.action_registration._is_forged_or_autonomous_turn", _boom)
    ctx = _ctx()
    # Fail-closed -> returns False immediately. Fail-open (pre-fix) -> creates an
    # ask and polls forever, so the wait_for would time out instead.
    result = await asyncio.wait_for(
        provider.request("x402_request", {"amount_usd": 5}, ctx), timeout=0.4)
    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []
