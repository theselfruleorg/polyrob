"""068 X4: x402_pay is reachable — and bounded.

Until 2026-09-26 no prod session ever held `x402_pay`: the autonomous grant
carried only `x402_invoice`, `load_tool` answered "gated: money", and the spend
gate refused every goal/cron turn. The agent could not pay for one API call.
"""
import types

import pytest

from tools.x402 import spend_gate
from tools.x402.spend_gate import x402_spend_refusal


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


def _ctx(**kw):
    base = {"is_sub_agent": False, "role": "orchestrator", "metadata": {},
            "session_id": "sess-1", "user_id": "owner"}
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def goal_turn(monkeypatch):
    """A goal/cron-dispatched turn on the main agent: forged-shaped, admitted."""
    monkeypatch.setattr(spend_gate, "_forged_fn", lambda c, t: True)
    import tools.controller.turn_origin as to
    monkeypatch.setattr(to, "_is_autonomous_goal_turn", lambda c, t: True)


# ---- the gate -------------------------------------------------------------

def test_an_autonomous_goal_turn_may_pay_a_micro_payment(goal_turn, monkeypatch):
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    assert x402_spend_refusal(_ctx(), None, max_amount_usd=0.05) is None


def test_above_the_ceiling_it_relies_on_the_owner_queue_hook(goal_turn, monkeypatch):
    """The verb is on the payment-approval hook, so an above-ceiling autonomous
    payment reaches this tool only after the owner's tap."""
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    from core.config_policy import PAYMENT_APPROVAL_TOOLS
    assert "x402_pay_x402_fetch" in set(PAYMENT_APPROVAL_TOOLS)
    assert x402_spend_refusal(_ctx(), None, max_amount_usd=5.0) is None


def test_above_the_ceiling_without_the_hook_is_refused(goal_turn, monkeypatch):
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    import core.config_policy as cp
    monkeypatch.setattr(cp, "PAYMENT_APPROVAL_TOOLS", frozenset(), raising=False)
    why = x402_spend_refusal(_ctx(), None, max_amount_usd=5.0)
    assert why and "autonomous" in why


def test_the_hook_does_exempt_only_micro_payments(monkeypatch):
    monkeypatch.setenv("X402_AUTONOMOUS_MAX_USD", "1.0")
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": 0.5})
    assert spend_exemption("x402_pay_x402_fetch", {"max_amount_usd": 1.5}) is None


@pytest.mark.parametrize("ctx", [
    _ctx(role="leaf"),
    _ctx(is_sub_agent=True),
    _ctx(metadata={"turn_kind": "self_wake"}),
    _ctx(metadata={"turn_kind": "delegation_result"}),
])
def test_forged_origins_stay_refused(ctx):
    why = x402_spend_refusal(ctx, None, max_amount_usd=0.01)
    assert why and "forged" in why


def test_an_autonomous_turn_that_is_not_a_goal_run_is_refused(monkeypatch):
    monkeypatch.setattr(spend_gate, "_forged_fn", lambda c, t: True)
    import tools.controller.turn_origin as to
    monkeypatch.setattr(to, "_is_autonomous_goal_turn", lambda c, t: False)
    assert x402_spend_refusal(_ctx(), None, max_amount_usd=0.01)


# ---- the grant -----------------------------------------------------------

def _arm(monkeypatch, *, x402=True, wallet=True):
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "true")
    monkeypatch.setenv("X402_CLIENT_ENABLED", "true" if x402 else "false")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true" if wallet else "false")


def test_an_armed_autonomous_run_holds_x402_pay(monkeypatch):
    from agents.task.constants import autonomous_mode_tools
    _arm(monkeypatch)
    assert "x402_pay" in autonomous_mode_tools()


@pytest.mark.parametrize("x402,wallet", [(False, True), (True, False)])
def test_not_requested_when_the_tool_cannot_be_constructed(monkeypatch, x402, wallet):
    from agents.task.constants import autonomous_mode_tools
    _arm(monkeypatch, x402=x402, wallet=wallet)
    assert "x402_pay" not in autonomous_mode_tools()


def test_not_granted_unless_money_autonomy_is_armed(monkeypatch):
    from agents.task.constants import autonomous_mode_tools
    _arm(monkeypatch)
    monkeypatch.setenv("DEFI_AGENT_AUTONOMY", "false")
    assert "x402_pay" not in autonomous_mode_tools()


def test_an_agent_written_goal_still_cannot_request_it():
    """The grant rides the ARMED autonomous toolset, not the self-goal ceiling:
    goal_create keeps stripping money-spend tools from what the agent writes."""
    from core.config_policy.profiles import PROFILES
    assert "x402_pay" not in PROFILES["ceiling:self_goal"]
    assert "x402_pay" not in PROFILES["grant:autonomous"]


# ---- the owner verb ----------------------------------------------------------

class _Result:
    def __init__(self, content=None, error=None):
        self.extracted_content, self.error = content, error


class _FakeTool:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def x402_quote(self, params, ctx):
        _FakeTool.calls.append(("quote", params.url, ctx))
        return _Result(f"{params.url} requires x402 payment of $0.0500 USD")

    async def x402_fetch(self, params, ctx):
        _FakeTool.calls.append(("fetch", params.max_amount_usd, ctx))
        return _Result("x" * 5000)


@pytest.fixture
def fake_tool(monkeypatch):
    import tools.x402.service as svc
    _FakeTool.calls = []
    monkeypatch.setattr(svc, "X402PayTool", _FakeTool)
    return _FakeTool


URL = "https://api.example.com/v1/data"


@pytest.mark.asyncio
async def test_bare_pay_only_quotes(fake_tool):
    from surfaces.telegram.pay_ops import pay_reply
    out = await pay_reply("owner", [URL, "0.10"])
    assert fake_tool.calls[0][0] == "quote" and "go" in out
    assert not [c for c in fake_tool.calls if c[0] == "fetch"]


@pytest.mark.asyncio
async def test_go_without_a_max_never_pays(fake_tool):
    from surfaces.telegram.pay_ops import pay_reply
    out = await pay_reply("owner", [URL, "go"])
    assert "max_usd" in out and not fake_tool.calls


@pytest.mark.asyncio
async def test_go_pays_as_the_owner_and_bounds_the_body(fake_tool):
    from surfaces.telegram.pay_ops import pay_reply
    out = await pay_reply("owner", [URL, "$0.10", "go"])
    kind, amount, ctx = fake_tool.calls[0]
    assert kind == "fetch" and amount == 0.10
    assert ctx.role == "owner" and ctx.user_id == "owner" and not ctx.is_sub_agent
    assert "more characters" in out and len(out) < 1700


@pytest.mark.asyncio
async def test_no_owner_no_payment(fake_tool):
    from surfaces.telegram.pay_ops import pay_reply
    assert "owner" in (await pay_reply(None, [URL, "1", "go"])).lower()
    assert not fake_tool.calls


def test_pay_is_one_verb_on_every_seat():
    import core.money_verbs  # noqa: F401  (registers the money rows)
    from core.surfaces.dispatcher import _COMMANDS
    from core.verbs import VERB_TABLE
    from surfaces.telegram.harness import _ROOM_REFUSED_COMMANDS
    assert "/pay" in _COMMANDS and "/pay" in _ROOM_REFUSED_COMMANDS
    assert any(v.name == "/pay" and v.group == "money" for v in VERB_TABLE)


# ---- Codex B9: each /pay command is its own payment, a re-delivery is not ------

class _KeyTool:
    keys: list = []

    async def x402_fetch(self, params, ctx=None):
        from tools.x402.service import x402_idempotency_key
        _KeyTool.keys.append(x402_idempotency_key(params))
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="DATA")


@pytest.mark.asyncio
async def test_pay_keys_by_the_command_identity(monkeypatch):
    import tools.x402.service as svc
    from surfaces.telegram.pay_ops import pay_reply
    _KeyTool.keys = []
    monkeypatch.setattr(svc, "X402PayTool", _KeyTool)
    await pay_reply("owner", [URL, "0.10", "go"], request_id="tg:1001")
    await pay_reply("owner", [URL, "0.10", "go"], request_id="tg:1001")   # re-delivery
    await pay_reply("owner", [URL, "0.10", "go"], request_id="tg:1002")   # tomorrow's /pay
    await pay_reply("owner", [URL, "0.10", "go", "id=daily-report"], request_id="tg:1003")
    a, b, c, d = _KeyTool.keys
    assert a == b and a != c and d.endswith(":rid=daily-report")


def test_the_telegram_seat_passes_the_update_id():
    import inspect
    import surfaces.telegram.harness as h
    src = inspect.getsource(h._handle_owner_admin)
    assert "request_id=(f\"tg:{_key}\"" in src
