"""Task 9 / G-2 — PAYMENT_APPROVAL_MODE wires PAYMENT_APPROVAL_TOOLS (x402_request)
independent of the generic APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER seam:

  - mode="approve" (default): the payment tool routes through the durable
    `owner_queue` provider regardless of whether the operator ever set
    APPROVAL_REQUIRED_TOOLS.
  - mode="auto": the payment tool is NOT queued (the generic seam, still off by
    default, governs whether it's gated at all); a post-execution hook instead
    fires an owner notification + audit event for a within-cap creation, and does
    nothing for a rejected (over-cap) one.
  - the generic APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER mechanism is unaffected
    for a non-payment action.
  - forged/leaf/correspondent-tainted turns still cannot create a payment request
    (existing gates unchanged) even with mode="approve" wired in.
"""
import asyncio
import types

import pytest

import agents.task.constants as constants
import tools.controller.approval as approval
import tools.controller.approval_queue  # noqa: F401 — pre-import so its module-level
# register_approval_provider("owner_queue", ...) has ALREADY run before any test
# monkeypatches approval._PROVIDERS["owner_queue"] — otherwise Controller.__init__'s
# lazy `import tools.controller.approval_queue` would (on a cold import) re-run that
# registration AFTER the monkeypatch and clobber the test's spy provider back to
# the real OwnerQueueApprover.
from tools.controller.types import ActionResult


def _make_controller(tmp_path, user_id="u1", tainted=False):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(
        session_id="s1", user_id=user_id, workspace_dir=str(tmp_path),
        _correspondent_tainted=tainted,
    )
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


class _SpyProvider(approval.ApprovalProvider):
    calls = []
    outcome = True

    async def request(self, action_name, params, context):
        _SpyProvider.calls.append((action_name, dict(params or {})))
        return _SpyProvider.outcome


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("PAYMENT_APPROVAL_MODE", "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
              "APPROVAL_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()
    _SpyProvider.calls = []
    _SpyProvider.outcome = True
    yield
    constants._refreeze_compute_posture_for_tests()
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()


# --- mode="approve" (default) -------------------------------------------------------

def test_default_mode_is_approve():
    assert constants.payment_approval_mode() == "approve"


def test_mode_approve_routes_x402_request_through_owner_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None))

    assert reason is None  # the spy provider approved
    assert _SpyProvider.calls == [("x402_request", {"amount_usd": 5})]


def test_mode_approve_wires_the_money_specific_timeout(tmp_path, monkeypatch):
    """The registered hook honors `payment_approval_timeout_sec()` (300s default,
    an explicit APPROVAL_TIMEOUT_SEC still wins) — NOT the generic 30s default —
    proven end-to-end with a provider that never resolves."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "0.05")  # fast for the test
    constants._refreeze_payment_approval_flags_for_tests()

    class _NeverResolves(approval.ApprovalProvider):
        async def request(self, action_name, params, context):
            await asyncio.sleep(5)
            return True

    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _NeverResolves)
    c = _make_controller(tmp_path)

    reason = asyncio.run(asyncio.wait_for(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None), timeout=2))

    assert reason is not None and "timeout" in reason.lower()


def test_mode_approve_denies_when_owner_queue_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)
    _SpyProvider.outcome = False

    c = _make_controller(tmp_path)
    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None))

    assert reason is not None and "x402_request" in reason


def test_mode_approve_leaves_non_payment_tools_ungated_by_default(tmp_path, monkeypatch):
    """The generic APPROVAL_REQUIRED_TOOLS seam is untouched: with it unset, a
    non-payment action is still never gated, mode="approve" notwithstanding."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    reason = asyncio.run(c._run_pre_tool_call_hooks("read_file", {}, None))

    assert reason is None
    assert _SpyProvider.calls == []  # owner_queue was never even consulted


def test_generic_seam_still_gates_a_non_payment_tool_when_configured(tmp_path, monkeypatch):
    """APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER keep working unchanged for a
    non-payment action, independent of the payment mode block."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "git_push")
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    approval._refreeze_approval_flags_for_tests()
    constants._refreeze_payment_approval_flags_for_tests()

    c = _make_controller(tmp_path)
    reason = asyncio.run(c._run_pre_tool_call_hooks("git_push", {}, None))

    assert reason is not None and "git_push" in reason


# --- mode="auto" ---------------------------------------------------------------------

def test_mode_auto_does_not_queue_x402_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None))

    assert reason is None
    assert _SpyProvider.calls == []  # never queued through owner_queue


def test_mode_auto_notifies_and_audits_within_cap_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path)
    result = ActionResult(extracted_content="ok", metadata={
        "request_id": "inv_abc123", "amount_usd": 5.0, "purpose": "consulting"})
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1")
    asyncio.run(c._run_post_tool_call_hooks("x402_request", {"amount_usd": 5}, result, ctx))

    assert len(notified) == 1
    assert notified[0][0] == "u1"
    assert "inv_abc123" in notified[0][1] and "5.00" in notified[0][1]


def test_mode_auto_over_cap_rejection_never_notifies(tmp_path, monkeypatch):
    """The invoicing caps (modules/x402/invoicing.py) already reject an over-cap
    request as result.error — auto mode must never notify/audit that as a success."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path)
    result = ActionResult(error="x402_request refused: amount $999.00 exceeds the "
                                "invoice ceiling $50.00 (X402_INVOICE_MAX_USD)")
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1")
    asyncio.run(c._run_post_tool_call_hooks("x402_request", {"amount_usd": 999}, result, ctx))

    assert notified == []


# --- T7 review fix (Important finding): mode="auto" never loosens SPEND-side -------
#
# PAYMENT_APPROVAL_TOOLS also holds the four live-trade order verbs (L9). The
# original T7 cut let mode="auto" act-and-report those too (no pre-approval, just a
# misleading post-hoc notify) -- crossing the hard product line that trading is
# never act-and-report. This is UNCONDITIONAL: it applies even to an EXPLICIT
# PAYMENT_APPROVAL_MODE=auto (a deliberate behavior change for any deployment that
# had set it, per the finding's fix instructions), not just the full-autonomy
# default covered in test_autonomy_mode_approvals.py.

_TRADE_VERBS = (
    "hyperliquid_place_limit_order", "hyperliquid_place_market_order",
    "polymarket_place_limit_order", "polymarket_place_market_order",
)


def test_mode_auto_still_queues_trade_verbs_through_owner_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    for verb in _TRADE_VERBS:
        _SpyProvider.calls = []
        reason = asyncio.run(c._run_pre_tool_call_hooks(verb, {"amount_usd": 5}, None))
        assert reason is None, verb  # the spy (owner_queue) approved
        assert (verb, {"amount_usd": 5}) in _SpyProvider.calls, verb


def test_mode_auto_trade_verb_denied_when_owner_queue_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)
    _SpyProvider.outcome = False

    c = _make_controller(tmp_path)
    reason = asyncio.run(c._run_pre_tool_call_hooks(
        "hyperliquid_place_limit_order", {"amount_usd": 5}, None))

    assert reason is not None and "hyperliquid_place_limit_order" in reason


def test_mode_auto_trade_verb_wires_the_money_specific_timeout(tmp_path, monkeypatch):
    """Mirrors test_mode_approve_wires_the_money_specific_timeout -- the spend lane
    wired under mode="auto" uses the SAME money-appropriate 300s default (an
    explicit APPROVAL_TIMEOUT_SEC still wins), not the generic 30s default."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "0.05")  # fast for the test
    constants._refreeze_payment_approval_flags_for_tests()

    class _NeverResolves(approval.ApprovalProvider):
        async def request(self, action_name, params, context):
            await asyncio.sleep(5)
            return True

    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _NeverResolves)
    c = _make_controller(tmp_path)

    reason = asyncio.run(asyncio.wait_for(
        c._run_pre_tool_call_hooks(
            "hyperliquid_place_market_order", {"amount_usd": 5}, None), timeout=2))

    assert reason is not None and "timeout" in reason.lower()


def test_mode_approve_also_queues_trade_verbs_unchanged(tmp_path, monkeypatch):
    """Pin: mode="approve" is UNCHANGED by this fix -- it already queued every
    PAYMENT_APPROVAL_TOOLS member (receive AND spend) through owner_queue."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)
    for verb in _TRADE_VERBS:
        _SpyProvider.calls = []
        reason = asyncio.run(c._run_pre_tool_call_hooks(verb, {"amount_usd": 5}, None))
        assert reason is None, verb
        assert (verb, {"amount_usd": 5}) in _SpyProvider.calls, verb


def test_mode_auto_notify_hook_wired_with_receive_subset_only(tmp_path, monkeypatch):
    """The auto-notify post-hook (act-and-report) is wired ONLY with
    PAYMENT_RECEIVE_APPROVAL_TOOLS; the spend-lane pre-hook is wired with exactly
    the complement (PAYMENT_APPROVAL_TOOLS minus the receive subset). Proves the
    wiring split end-to-end through Controller.__init__, not just the pure
    constants-level set math."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()

    import tools.controller.approval_queue as approval_queue_mod

    notify_calls = []
    orig_notify = approval_queue_mod.make_payment_auto_notify_hook

    def _spy_notify(container, tools, taint_probe=None):
        notify_calls.append(set(tools))
        return orig_notify(container, tools, taint_probe=taint_probe)

    pre_calls = []
    orig_approval_hook = approval.make_approval_hook

    def _spy_approval_hook(provider, tools, **kw):
        pre_calls.append(set(tools))
        return orig_approval_hook(provider, tools, **kw)

    monkeypatch.setattr(approval_queue_mod, "make_payment_auto_notify_hook", _spy_notify)
    monkeypatch.setattr(approval, "make_approval_hook", _spy_approval_hook)

    _make_controller(tmp_path)

    assert notify_calls == [{"x402_request"}]
    assert pre_calls == [set(_TRADE_VERBS)]


# --- regression: existing gates unchanged --------------------------------------------

def test_correspondent_tainted_turn_still_cannot_create_payment_request(tmp_path, monkeypatch):
    """Even with PAYMENT_APPROVAL_MODE=approve wired (owner_queue wants to queue
    it), a correspondent-tainted turn must still be denied outright — the
    pre-existing correspondent-gate hook (registered by agent construction, not
    Controller.__init__) must keep firing regardless of hook registration order."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path)

    from agents.task.agent.core.correspondent_gate import make_correspondent_gate_hook
    gate = make_correspondent_gate_hook(lambda: True, resolve_tool=None)
    c.register_pre_tool_call_hook(gate, fail_mode="closed")

    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None))

    assert reason is not None
    assert "untrusted correspondent" in reason or "blocked" in reason.lower()


def test_leaf_turn_denied_by_owner_queue_itself(tmp_path, monkeypatch):
    """Belt-and-suspenders: OwnerQueueApprover itself refuses a forged/leaf turn
    (tools/controller/approval_queue.py) without ever creating a durable ask."""
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.approval_queue import OwnerQueueApprover
    from tools.controller.execution_context import ActionExecutionContext

    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = OwnerQueueApprover(board=board)
    ctx = ActionExecutionContext(session_id="s1", user_id="u1", role="leaf", is_sub_agent=True)

    result = asyncio.run(provider.request("x402_request", {"amount_usd": 5}, ctx))

    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


# --- fix pass 1 / Finding 1: correspondent-taint short-circuit -----------------------
#
# The pre-existing correspondent_gate hook (test above) is registered LATER, in agent
# construction — after owner_queue is already wired in Controller.__init__. Without its
# OWN taint short-circuit, a correspondent-tainted turn that gets the LLM to attempt
# x402_request would create a durable ask + push a real owner notification + block up
# to payment_approval_timeout_sec() (300s default), purely because owner_queue's
# pre-hook runs BEFORE correspondent_gate's. These tests exercise OwnerQueueApprover's
# own taint_probe directly (unit-level) and the Controller wiring that supplies it
# from the orchestrator's `_correspondent_tainted` flag (integration-level).

def test_taint_probe_true_denies_without_creating_ask_or_notifying(tmp_path, monkeypatch):
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.approval_queue import OwnerQueueApprover
    from tools.controller.execution_context import ActionExecutionContext

    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = OwnerQueueApprover(board=board, taint_probe=lambda: True)
    ctx = ActionExecutionContext(
        session_id="s1", user_id="u1", role="orchestrator", is_sub_agent=False)

    result = asyncio.run(provider.request("x402_request", {"amount_usd": 5}, ctx))

    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []
    assert notified == []


def test_taint_probe_raising_fails_closed(tmp_path):
    """A probe that raises must be treated as tainted — we can't prove the turn is
    clean, so deny rather than fail-open into creating an ask."""
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.approval_queue import OwnerQueueApprover
    from tools.controller.execution_context import ActionExecutionContext

    def _boom():
        raise RuntimeError("probe exploded")

    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = OwnerQueueApprover(board=board, taint_probe=_boom)
    ctx = ActionExecutionContext(
        session_id="s1", user_id="u1", role="orchestrator", is_sub_agent=False)

    result = asyncio.run(provider.request("x402_request", {"amount_usd": 5}, ctx))

    assert result is False
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


def test_taint_probe_false_creates_ask_as_before(tmp_path, monkeypatch):
    """An untainted turn is unaffected by the taint_probe wiring — a durable ask is
    still created exactly as before, and an owner decision still resolves it."""
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.approval_queue import OwnerQueueApprover
    from tools.controller.execution_context import ActionExecutionContext

    async def _fake_notify(container, user_id, text):
        pass

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    board = GoalBoard(str(tmp_path / "goals.db"))
    provider = OwnerQueueApprover(board=board, taint_probe=lambda: False, poll_interval=0.01)
    ctx = ActionExecutionContext(
        session_id="s1", user_id="u1", role="orchestrator", is_sub_agent=False)

    async def _run_and_approve():
        task = asyncio.ensure_future(
            provider.request("x402_request", {"amount_usd": 5}, ctx))
        for _ in range(50):
            await asyncio.sleep(0.01)
            asks = board.asks(user_id="u1", status=ASK_OPEN)
            if asks:
                break
        else:
            raise AssertionError("ask was never created")
        board.decide_ask(asks[0].id, user_id="u1", approved=True)
        return await task

    result = asyncio.run(_run_and_approve())
    assert result is True


def test_owner_queue_wiring_denies_when_orchestrator_tainted(tmp_path, monkeypatch):
    """Controller.__init__ wires OwnerQueueApprover's taint_probe from the SAME
    orchestrator._correspondent_tainted flag correspondent_gate reads — proving the
    fix end-to-end through the real construction path (not just direct construction)."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.execution_context import ActionExecutionContext

    c = _make_controller(tmp_path, tainted=True)
    ctx = ActionExecutionContext(
        session_id="s1", user_id="u1", role="orchestrator", is_sub_agent=False)
    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, ctx))

    assert reason is not None and "x402_request" in reason
    assert notified == []
    board = GoalBoard(str(tmp_path / "goals.db"))
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


def test_owner_queue_wiring_unaffected_when_orchestrator_untainted(tmp_path, monkeypatch):
    """Control: an untainted orchestrator still routes through owner_queue exactly
    as before — the taint_probe wiring is a pure addition, not a behavior change."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    constants._refreeze_payment_approval_flags_for_tests()
    monkeypatch.setitem(approval._PROVIDERS, "owner_queue", _SpyProvider)

    c = _make_controller(tmp_path, tainted=False)
    reason = asyncio.run(
        c._run_pre_tool_call_hooks("x402_request", {"amount_usd": 5}, None))

    assert reason is None  # the spy provider approved
    assert _SpyProvider.calls == [("x402_request", {"amount_usd": 5})]


def test_mode_auto_tainted_turn_emits_no_notification(tmp_path, monkeypatch):
    """The same taint short-circuit applies to make_payment_auto_notify_hook: mode=
    auto emits no owner notification/audit event for a tainted turn either."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    constants._refreeze_payment_approval_flags_for_tests()
    notified = []

    async def _fake_notify(container, user_id, text):
        notified.append((user_id, text))

    monkeypatch.setattr(
        "tools.controller.approval_queue._push_owner_notification", _fake_notify)

    c = _make_controller(tmp_path, tainted=True)
    result = ActionResult(extracted_content="ok", metadata={
        "request_id": "inv_abc123", "amount_usd": 5.0, "purpose": "consulting"})
    ctx = types.SimpleNamespace(user_id="u1", session_id="s1")
    asyncio.run(c._run_post_tool_call_hooks("x402_request", {"amount_usd": 5}, result, ctx))

    assert notified == []
