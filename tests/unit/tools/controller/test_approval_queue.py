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


@pytest.fixture(autouse=True)
def _bound_owner(monkeypatch):
    """Approval requests in this module model the bound process owner."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")


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


# --- 039: an autonomous GOAL run may ASK, and may redeem a grant --------------------
#
# Live dead end, prod 2026-09-12. The bridge verb runs on caps-not-taps, so an
# autonomous goal run reaches it; above DEFI_AUTONOMOUS_MAX_USD it escalates to
# owner_queue — which denied every forged-shaped turn OUTRIGHT, creating no ask and
# never reaching _consume_grant. So an above-ceiling autonomous spend could not be
# approved by ANY route: no tap to press, and an approval given earlier could not be
# redeemed later either. The agent, holding no tap id, told the owner to
# `/approve <relay-request-id>` — a handle that does not exist.
#
# The ONE origin this opens is `_is_autonomous_goal_turn`: main agent, orchestrator
# role, not a sub-agent, not a self-wake or delegation-result re-entry, and carrying
# a live autonomy marker. It still cannot SELF-approve — it may only ASK, and redeem
# what the owner already granted.

def _goal_ctx(**kw):
    return _ctx(metadata={"turn_kind": None}, **kw)


@pytest.fixture
def autonomous_goal_turn(monkeypatch):
    """Make the strict detector answer True, as a real goal-dispatched run does."""
    monkeypatch.setattr("tools.controller.action_registration._is_forged_or_autonomous_turn",
                        lambda ctx, _s: True)
    monkeypatch.setattr("tools.controller.action_registration._is_autonomous_goal_turn",
                        lambda ctx, _s: True)


@pytest.mark.asyncio
async def test_an_autonomous_goal_run_CREATES_an_ask_instead_of_dead_ending(
        provider, board, autonomous_goal_turn):
    result = await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    assert result is False, "it may ask, never self-approve"
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1, "the owner must have something to press"
    assert asks[0].payload.get("tool_name") == "defi_trade_bridge"


@pytest.mark.asyncio
async def test_an_autonomous_goal_run_does_not_hold_a_dispatcher_slot(
        provider, board, autonomous_goal_turn):
    """It must return promptly rather than block the poll window — a goal run that
    sits on a dispatcher slot for the timeout starves every other goal, and the ask
    is durable anyway."""
    result = await asyncio.wait_for(
        provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx()),
        timeout=1.5)
    assert result is False


@pytest.mark.asyncio
async def test_an_owner_grant_is_REDEEMABLE_by_the_next_autonomous_run(
        provider, board, autonomous_goal_turn):
    """The half that made 'approve now, it re-fires later' impossible: the forged
    denial sat ABOVE _consume_grant, so an approval the owner had already given
    could never be spent by the run it was given for."""
    params = {"usd": 91.37}
    assert await provider.request("defi_trade_bridge", params, _goal_ctx()) is False
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    ok, _ = board.decide_ask(ask.id, user_id="u1", approved=True)
    assert ok is True

    # The next dispatch of the same goal, same params — must now fire.
    assert await provider.request("defi_trade_bridge", params, _goal_ctx()) is True


@pytest.mark.asyncio
async def test_the_grant_is_still_one_shot_for_an_autonomous_run(
        provider, board, autonomous_goal_turn):
    """One approval authorizes exactly ONE execution, autonomous or not."""
    params = {"usd": 91.37}
    await provider.request("defi_trade_bridge", params, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    board.decide_ask(ask.id, user_id="u1", approved=True)

    assert await provider.request("defi_trade_bridge", params, _goal_ctx()) is True
    assert await provider.request("defi_trade_bridge", params, _goal_ctx()) is False, (
        "a second execution must not ride one approval")


@pytest.mark.asyncio
async def test_a_grant_for_DIFFERENT_params_is_not_redeemable(
        provider, board, autonomous_goal_turn):
    await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    board.decide_ask(ask.id, user_id="u1", approved=True)
    assert await provider.request("defi_trade_bridge", {"usd": 910.0},
                                  _goal_ctx()) is False


@pytest.mark.asyncio
async def test_a_rejected_ask_leaves_nothing_to_redeem(
        provider, board, autonomous_goal_turn):
    params = {"usd": 91.37}
    await provider.request("defi_trade_bridge", params, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    board.decide_ask(ask.id, user_id="u1", approved=False)
    assert await provider.request("defi_trade_bridge", params, _goal_ctx()) is False


# --- the origins this must NOT open ------------------------------------------------

@pytest.mark.asyncio
async def test_a_leaf_is_still_denied_with_no_ask(provider, board, monkeypatch):
    monkeypatch.setattr("tools.controller.action_registration._is_forged_or_autonomous_turn",
                        lambda ctx, _s: True)
    monkeypatch.setattr("tools.controller.action_registration._is_autonomous_goal_turn",
                        lambda ctx, _s: False)
    assert await provider.request("defi_trade_bridge", {"usd": 5},
                                  _ctx(role="leaf", is_sub_agent=True)) is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


@pytest.mark.asyncio
async def test_a_self_wake_is_still_denied_with_no_ask(provider, board, monkeypatch):
    monkeypatch.setattr("tools.controller.action_registration._is_forged_or_autonomous_turn",
                        lambda ctx, _s: True)
    monkeypatch.setattr("tools.controller.action_registration._is_autonomous_goal_turn",
                        lambda ctx, _s: False)
    ctx = _ctx(metadata={"turn_kind": "self_wake"})
    assert await provider.request("defi_trade_bridge", {"usd": 5}, ctx) is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


@pytest.mark.asyncio
async def test_a_goal_detector_that_RAISES_keeps_the_denial(provider, board, monkeypatch):
    """Fail closed: an unprovable origin earns no ask."""
    monkeypatch.setattr("tools.controller.action_registration._is_forged_or_autonomous_turn",
                        lambda ctx, _s: True)

    def _boom(ctx, _s):
        raise RuntimeError("marker store unreadable")
    monkeypatch.setattr("tools.controller.action_registration._is_autonomous_goal_turn", _boom)
    assert await provider.request("defi_trade_bridge", {"usd": 5}, _goal_ctx()) is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


@pytest.mark.asyncio
async def test_a_tainted_autonomous_run_is_still_denied_with_no_ask(
        provider, board, autonomous_goal_turn):
    """Correspondent taint outranks the goal lane — a third party's reply must not
    be able to make the owner's phone buzz with a money ask."""
    provider.set_taint_probe(lambda: True)
    assert await provider.request("defi_trade_bridge", {"usd": 5}, _goal_ctx()) is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


# --- 039: approving the ask must RE-ARM the goal that raised it --------------------
#
# The second half of the same dead end. Creating an ask fixed "nothing to press";
# without stamping `blocks_goal_ids` the owner presses /approve and STILL nothing
# happens — the grant sits unredeemed and the goal stays blocked. That is the same
# "owner intent does not stick" failure one layer down.

@pytest.mark.asyncio
async def test_an_ask_from_a_goal_run_names_the_goal_it_blocks(
        provider, board, autonomous_goal_turn, monkeypatch):
    from agents.task.goals.board import STATUS_READY
    goal = board.create(user_id="u1", title="bridge to robinhood", status=STATUS_READY)
    monkeypatch.setattr("agents.task.goals.autonomy_marker.goal_for_session",
                        lambda sid: goal.id)

    await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    assert ask.payload.get("blocks_goal_ids") == [goal.id]


@pytest.mark.asyncio
async def test_approving_the_ask_flips_the_blocked_goal_back_to_ready(
        provider, board, autonomous_goal_turn, monkeypatch):
    """End to end: the owner presses /approve and the goal becomes runnable again,
    which is what makes the durable grant redeemable at all."""
    from agents.task.goals.board import STATUS_READY
    goal = board.create(user_id="u1", title="bridge to robinhood", status=STATUS_READY)
    board.record_failure(goal.id, error="agent declared BLOCKED: needs owner approval")
    board.update_status(goal.id, "blocked")
    monkeypatch.setattr("agents.task.goals.autonomy_marker.goal_for_session",
                        lambda sid: goal.id)

    await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    ok, unblocked = board.decide_ask(ask.id, user_id="u1", approved=True)
    assert ok is True
    assert unblocked >= 1, "approving must re-arm the goal that raised the ask"
    assert board.get(goal.id).status == STATUS_READY


@pytest.mark.asyncio
async def test_an_interactive_ask_blocks_no_goal(provider, board):
    """An owner-driven turn has no goal to re-arm; the field stays empty rather
    than pointing at whatever ran last."""
    ctx = _ctx()
    task = asyncio.create_task(provider.request("x402_request", {"amount_usd": 5}, ctx))
    await asyncio.sleep(0.06)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert asks[0].payload.get("blocks_goal_ids", []) == []
    board.decide_ask(asks[0].id, user_id="u1", approved=False)
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_missing_goal_mapping_still_creates_the_ask(
        provider, board, autonomous_goal_turn, monkeypatch):
    """Fail-open. An ask that does not auto-re-arm is recoverable by hand; an ask
    that was never created is the dead end this all exists to close."""
    def _boom(sid):
        raise RuntimeError("marker gone")
    monkeypatch.setattr("agents.task.goals.autonomy_marker.goal_for_session", _boom)
    assert await provider.request("defi_trade_bridge", {"usd": 5}, _goal_ctx()) is False
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1


def test_the_marker_remembers_which_goal_a_session_runs():
    from agents.task.goals import autonomy_marker as am
    am.mark_autonomous("sess-1", "goal-abc")
    assert am.is_autonomous("sess-1") is True
    assert am.goal_for_session("sess-1") == "goal-abc"
    # A cron/planner run is autonomous with no goal row — a real answer, not a gap.
    am.mark_autonomous("sess-2")
    assert am.is_autonomous("sess-2") is True
    assert am.goal_for_session("sess-2") is None
    assert am.goal_for_session("never-seen") is None


@pytest.mark.asyncio
async def test_re_asking_widens_the_goal_list_on_an_EXISTING_ask(
        provider, board, autonomous_goal_turn, monkeypatch):
    """owner_queue runs its own exact-hash dedup, so a re-ask REUSES the open ask
    and never reached create_ask's merge. An ask created before the goal list
    existed - or by a run that had no goal - would therefore re-arm nothing
    forever, no matter how many times it was asked again."""
    from agents.task.goals.board import STATUS_READY
    goal = board.create(user_id="u1", title="bridge", status=STATUS_READY)

    # First ask: no goal mapping (e.g. raised before 039, or by a cron run).
    monkeypatch.setattr("agents.task.goals.autonomy_marker.goal_for_session",
                        lambda sid: None)
    await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    assert ask.payload.get("blocks_goal_ids") == []

    # Same request, now from a run that knows its goal: the SAME ask is reused.
    monkeypatch.setattr("agents.task.goals.autonomy_marker.goal_for_session",
                        lambda sid: goal.id)
    await provider.request("defi_trade_bridge", {"usd": 91.37}, _goal_ctx())
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1, "must not create a second tap for one action"
    assert asks[0].payload.get("blocks_goal_ids") == [goal.id]


def test_add_ask_blocked_goals_is_idempotent_and_never_raises(tmp_path):
    from agents.task.goals.board import GoalBoard, STATUS_READY
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="u1", title="x", status=STATUS_READY)
    ask = b.create_ask(user_id="u1", what="Approve?", why="because",
                       blocks_goal_ids=[g.id], force=True)
    assert b.add_ask_blocked_goals(ask.id, [g.id]) == [g.id]
    assert b.add_ask_blocked_goals(ask.id, []) == []
    assert b.add_ask_blocked_goals("no-such-ask", [g.id]) == []


# --- 039: the grant must survive a RE-QUOTE ----------------------------------------

@pytest.mark.asyncio
async def test_a_requote_reuses_the_SAME_tap_when_the_intent_is_unchanged(
        provider, board, autonomous_goal_turn):
    """The owner answered THREE taps for one bridge on 2026-09-12. Every attempt
    re-quoted, so min_out/usd/request_id moved, the request hash moved with them,
    and each try minted a brand-new ask."""
    intent = {"from_chain": "base", "to_chain": "robinhood", "amount": 0.036,
              "token_out": "native", "recipient": "0xcAda"}
    await provider.request("defi_trade_bridge",
                           {"usd": 91.37, "min_out": 0.03526, "request_id": "0xaaa"},
                           _goal_ctx(), hash_params=intent)
    await provider.request("defi_trade_bridge",
                           {"usd": 91.54, "min_out": 0.03527, "request_id": "0xbbb"},
                           _goal_ctx(), hash_params=intent)
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1, (
        "one intent must mean one tap, however many times it is re-quoted")


@pytest.mark.asyncio
async def test_an_approval_survives_the_requote_and_is_redeemed(
        provider, board, autonomous_goal_turn):
    """The whole point: approve now, and the NEXT run — which re-quotes and gets
    different numbers — still redeems it."""
    intent = {"from_chain": "base", "to_chain": "robinhood", "amount": 0.036,
              "token_out": "native", "recipient": "0xcAda"}
    await provider.request("defi_trade_bridge",
                           {"usd": 91.37, "min_out": 0.03526, "request_id": "0xaaa"},
                           _goal_ctx(), hash_params=intent)
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    board.decide_ask(ask.id, user_id="u1", approved=True)

    # A later dispatch: fresh quote, different figures, same intent.
    assert await provider.request(
        "defi_trade_bridge",
        {"usd": 92.10, "min_out": 0.03531, "request_id": "0xccc"},
        _goal_ctx(), hash_params=intent) is True


@pytest.mark.asyncio
async def test_a_CHANGED_intent_still_needs_its_own_approval(
        provider, board, autonomous_goal_turn):
    """Stability must not become blindness: a different amount, asset, chain or
    recipient is a different decision and gets its own tap."""
    base = {"from_chain": "base", "to_chain": "robinhood", "amount": 0.036,
            "token_out": "native", "recipient": "0xcAda"}
    await provider.request("defi_trade_bridge", {"usd": 91.0}, _goal_ctx(),
                           hash_params=base)
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    board.decide_ask(ask.id, user_id="u1", approved=True)

    for changed in ({**base, "amount": 0.36}, {**base, "recipient": "0xEVIL"},
                    {**base, "to_chain": "ethereum"}, {**base, "token_out": "weth"}):
        assert await provider.request("defi_trade_bridge", {"usd": 91.0},
                                      _goal_ctx(), hash_params=changed) is False, changed


@pytest.mark.asyncio
async def test_the_owner_still_SEES_the_live_figures(provider, board,
                                                     autonomous_goal_turn):
    """Keying on the intent must not hide the price he is approving."""
    await provider.request("defi_trade_bridge",
                           {"usd": 91.37, "min_out": 0.03526}, _goal_ctx(),
                           hash_params={"amount": 0.036})
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    assert "91.37" in str(ask.payload.get("params_summary"))


@pytest.mark.asyncio
async def test_omitting_hash_params_keys_on_the_displayed_params(provider, board):
    """Every existing caller passes no hash_params and must be unaffected."""
    ctx = _ctx()
    task = asyncio.create_task(provider.request("x402_request", {"amount_usd": 5}, ctx))
    await asyncio.sleep(0.06)
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert asks[0].payload["request_hash"] == compute_request_hash(
        "x402_request", {"amount_usd": 5}, "u1")
    board.decide_ask(asks[0].id, user_id="u1", approved=False)
    await asyncio.wait_for(task, timeout=2)


# --- 039: the wake must not race the path that can actually spend ------------------
#
# Live on prod 2026-09-12. The owner approved a bridge; resume-on-grant woke the
# session with "Retry it now"; tx_guard step 2 refused it — "forged/autonomous
# turns cannot move funds, even with the grant". A self-wake turn is structurally
# barred from spending, so the wake instructed the agent to do the one thing that
# turn cannot do, and burned the attempt doing it.

class _WakeSpy:
    def __init__(self):
        self.calls = []

    def route_session(self, session_id):
        # 043 W10: model a SAME-process agent that OWNS the session (resident
        # here) — the only case an in-process self-wake is correct.
        from agents.task.session_route import LOCAL, SessionRoute
        return SessionRoute(status=LOCAL, orchestrator=object())

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.calls.append((session_id, text, metadata))


@pytest.mark.asyncio
async def test_no_self_wake_when_a_goal_is_being_re_armed(board):
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    from agents.task.goals.board import STATUS_READY
    goal = board.create(user_id="u1", title="bridge", status=STATUS_READY)
    board.record_failure(goal.id, error="blocked on approval")
    board.update_status(goal.id, "blocked")
    ask = board.create_ask(user_id="u1", what="Approve defi_trade_bridge?",
                           why="x", blocks_goal_ids=[goal.id], force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "defi_trade_bridge",
                                          "session_id": "s1"})
    spy = _WakeSpy()
    ok, msg = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                   approved=True, task_agent=spy)
    assert ok is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert spy.calls == [], (
        "a self-wake cannot spend; the re-armed goal is the redemption path")
    assert board.get(goal.id).status == STATUS_READY


@pytest.mark.asyncio
async def test_an_interactive_ask_still_wakes_but_promises_nothing_it_cannot_do(board):
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    ask = board.create_ask(user_id="u1", what="Approve x402_request?", why="x",
                           force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "x402_request",
                                          "session_id": "s1"})
    spy = _WakeSpy()
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=True, task_agent=spy)
    assert ok is True
    await asyncio.sleep(0)   # the wake is fire-and-forget; let it be scheduled
    await asyncio.sleep(0)
    assert len(spy.calls) == 1
    text = spy.calls[0][1]
    assert "grant is live" in text
    assert "Retry it now" not in text, (
        "that instruction is what produced the approved-but-blocked loop")
    assert "Do not retry in a loop" in text


@pytest.mark.asyncio
async def test_a_rejection_never_wakes(board):
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    ask = board.create_ask(user_id="u1", what="Approve x?", why="x", force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "x402_request",
                                          "session_id": "s1"})
    spy = _WakeSpy()
    decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                         approved=False, task_agent=spy)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert spy.calls == []


# --- 2026-09-18: a CRON run's ask re-arms the JOB, never wakes the dead session --
#
# Live on prod 2026-09-17 16:13-16:15. The buyback-ladder cron run hit the owner
# queue, released its slot, and the owner tapped approve 56 s later. The ask had
# no goal to re-arm, so resume-on-grant woke the FINISHED cron session — a
# self-wake, a forged turn the money guard refuses — and the agent told the owner
# "trigger it from your seat" for a trade the owner had just approved. The next
# scheduled tick would have redeemed the grant, hours later. Now the ask names
# its job and an approval pulls that job to the next tick.

def _cron_store(tmp_path):
    from datetime import datetime, timedelta, timezone
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    store.add(CronJob(id="job-buyback", task="buyback tranche", schedule_spec="every 4h",
                      user_id="u1", next_run_at=later))
    return store, later


@pytest.mark.asyncio
async def test_an_ask_from_a_cron_run_names_its_job(
        provider, board, autonomous_goal_turn, monkeypatch):
    from agents.task.goals import autonomy_marker as am
    am.mark_autonomous("s-cron", None, cron_job_id="job-buyback")
    await provider.request("defi_trade_swap", {"amount_in": 0.04},
                           _goal_ctx(session_id="s-cron"))
    ask = board.asks(user_id="u1", status=ASK_OPEN)[0]
    assert ask.payload.get("cron_job_id") == "job-buyback"
    assert ask.payload.get("blocks_goal_ids") == []


@pytest.mark.asyncio
async def test_approving_a_cron_ask_re_arms_the_job_and_never_wakes(board, tmp_path):
    from datetime import datetime, timezone
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    store, later = _cron_store(tmp_path)   # beside goals.db, as on a real data home
    ask = board.create_ask(user_id="u1", what="Approve defi_trade_swap?", why="x",
                           force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "defi_trade_swap",
                                          "session_id": "s-cron",
                                          "cron_job_id": "job-buyback"})
    spy = _WakeSpy()
    ok, msg = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                   approved=True, task_agent=spy)
    assert ok is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert spy.calls == [], "a self-wake into a finished cron run cannot spend"
    job = store.get("job-buyback")
    assert job.next_run_at <= datetime.now(timezone.utc), "the job must be due now"
    assert job.next_run_at < later
    assert "next tick" in msg


@pytest.mark.asyncio
async def test_a_running_cron_job_is_not_re_armed(board, tmp_path):
    """A CAS on status: a job mid-run keeps its row; the grant stays live for
    its own next run and the owner is told so."""
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    store, later = _cron_store(tmp_path)
    assert store.claim_for_run("job-buyback")
    ask = board.create_ask(user_id="u1", what="Approve defi_trade_swap?", why="x",
                           force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "defi_trade_swap",
                                          "session_id": "s-cron",
                                          "cron_job_id": "job-buyback"})
    ok, msg = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                   approved=True, task_agent=_WakeSpy())
    assert ok is True
    assert store.get("job-buyback").next_run_at == later
    assert "next run redeems" in msg


@pytest.mark.asyncio
async def test_rejecting_a_cron_ask_leaves_the_job_alone(board, tmp_path):
    from tools.controller.approval_queue import decide_tool_approval, tap_display_id
    store, later = _cron_store(tmp_path)
    ask = board.create_ask(user_id="u1", what="Approve defi_trade_swap?", why="x",
                           force=True,
                           extra_payload={"ask_kind": "tool_approval",
                                          "tool_name": "defi_trade_swap",
                                          "session_id": "s-cron",
                                          "cron_job_id": "job-buyback"})
    decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                         approved=False, task_agent=_WakeSpy())
    assert store.get("job-buyback").next_run_at == later


def test_the_marker_remembers_which_cron_job_a_session_runs():
    from agents.task.goals import autonomy_marker as am
    am.mark_autonomous("s-c1", None, cron_job_id="job-1")
    am.mark_autonomous("s-g1", "goal-1")
    assert am.cron_job_for_session("s-c1") == "job-1"
    assert am.cron_job_for_session("s-g1") is None
    assert am.cron_job_for_session(None) is None
    assert am.goal_for_session("s-c1") is None
