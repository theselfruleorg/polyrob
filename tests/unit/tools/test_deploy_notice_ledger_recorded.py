"""A settled notice may claim a ledger record only after the record ran (043 T2).

Both money paths that 043 A36 deferred — `tools/defi/deploy_verb.py` and
`tools/launchpad/execute.py` — emitted their SETTLED owner notice with
`ledger_recorded=True` BEFORE `gate.record` was called. A36 moved the record
below the receipt/address read (the durable row must name what actually landed,
never the predicted address), so the notice was making a claim about something
that had not happened yet and could still fail.

`tx_notify.render_settled` prints "⚠ ledger: NOT recorded — this spend is
invisible to every other money verb's cap" on `ledger_recorded is False`, so the
field is exactly the kind of statement that must never be confident and wrong:
a `True` over a record that then raised leaves the owner reading a clean line
over a spend no cap can see.

What is pinned here is ORDER and TRUTH, never a new gate:

* the settled notice is emitted AFTER `gate.record`, and carries `True`;
* a raising `gate.record` still emits a settled notice, carrying `False` —
  never a silent `True`, and never no notice at all;
* the raise still propagates (an unrecorded spend stays a loud failure).
"""
import contextlib

import pytest

from core.wallet import tx_notify
from core.wallet.deploy_guard import DeployFacts
from core.wallet.tx_guard import Decision
from core.wallet import token_template

HOLDER = "0x2222222222222222222222222222222222222222"
CURVE = "0x526fce0f274615695073fd3a09a54f2646DF1E00"


class _Boom(RuntimeError):
    """What a ledger write failing looks like from the call site."""


class _Gate:
    """Records into a SHARED event log so ordering is observable."""

    def __init__(self, events, *, raises=False):
        self.events = events
        self.raises = raises
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.events.append(("record", kw.get("counterparty")))
        if self.raises:
            raise _Boom("ledger write failed")
        self.recorded.append(kw)


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    def __init__(self, chain, signer, **kw):
        self.sent = False

    def build_deploy(self, *, init_code, value=0):
        return {"to": None, "data": init_code, "value": value, "nonce": 4}

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "nonce": 4}

    def size_gas(self, tx, sim_gas_used):
        return {**tx, "gas": sim_gas_used * 3 // 2}

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ab" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=7,
                       gas_used=500_000)

    def _rpc(self, method, params, **kw):
        if method == "eth_getTransactionReceipt":
            return {"contractAddress": "0x" + "11" * 20, "logs": []}
        return "0x60806040"          # code IS present at a CREATE2 target


def _recorder(tool, events):
    """Replace `tool._notify_tx` with a recorder onto the shared event log."""

    def _notify(execution_context, notice, *, settled):
        events.append(("notice", settled, notice.ledger_recorded))

    tool._notify_tx = _notify
    return events


def _settled(events):
    return [e for e in events if e[0] == "notice" and e[1] is True]


def _index(events, kind):
    for i, e in enumerate(events):
        if e[0] == kind:
            return i
    return -1


# ==========================================================================
# tools/defi/deploy_verb.py
# ==========================================================================

@pytest.fixture
def _armed(monkeypatch):
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")


def _deploy_tool(events, *, raises=False):
    from tools.defi.trade_tool import DefiTradeTool

    gate = _Gate(events, raises=raises)
    facts = DeployFacts(predicted_address="0x" + "cd" * 20,
                        runtime_size=1686,
                        runtime_hash=token_template.RUNTIME_HASH,
                        template_matched=True)

    def _guard(intent, tx, **kw):
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=1.5, sim_gas_used=501_207, deploy=facts)

    tool = DefiTradeTool(wallet=_Wallet(gate), rail_factory=_Rail,
                         guard_fn=_guard, price_fn=lambda c, a: 2500.0,
                         fallback_price_fn=lambda c, a: None)
    _recorder(tool, events)
    return tool, gate


async def _run_deploy(tool):
    from tools.defi.trade_tool import DeployTokenParams
    return await tool.deploy_token(DeployTokenParams(
        chain="base", name="Ada", symbol="ADA", supply=1_000_000,
        max_spend_usd=5.0, dry_run=False))


@pytest.mark.asyncio
async def test_deploy_settled_notice_comes_after_the_record(_armed):
    events = []
    tool, gate = _deploy_tool(events)
    res = await _run_deploy(tool)

    assert "DEPLOYED AND CONFIRMED" in (res.extracted_content or ""), res.error
    assert gate.recorded, "the spend must still be recorded"
    settled = _settled(events)
    assert len(settled) == 1, f"exactly one settled notice, got {events}"
    assert settled[0][2] is True
    assert _index(events, "record") < events.index(settled[0]), (
        f"the settled notice claimed a record before it happened: {events}")


@pytest.mark.asyncio
async def test_deploy_record_failure_notifies_not_recorded(_armed):
    events = []
    tool, gate = _deploy_tool(events, raises=True)

    with pytest.raises(_Boom):
        await _run_deploy(tool)

    settled = _settled(events)
    assert len(settled) == 1, f"a failed record still notifies once: {events}"
    assert settled[0][2] is False, "never a silent True over a failed record"
    assert not gate.recorded


# ==========================================================================
# tools/launchpad/execute.py
# ==========================================================================

async def _run_launchpad(events, *, raises=False):
    from tools.launchpad import execute
    from tools.launchpad.tool import LaunchpadTool

    gate = _Gate(events, raises=raises)

    def _guard(intent, tx, **kw):
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=2.0, sim_gas_used=250_000)

    tool = LaunchpadTool("launchpad", config=None, container=None,
                         wallet=_Wallet(gate), rail_factory=_Rail,
                         guard_fn=_guard, price_fn=lambda c, a: 2500.0,
                         rpc_fn=lambda m, p: None)
    _recorder(tool, events)
    res = await execute.guarded_send(
        tool, execution_context=None, verb="buy", chain="robinhood",
        to=CURVE, calldata="0xdeadbeef", value_wei=10 ** 15,
        max_spend_usd=5.0, dry_run=False, header="  test\n")
    return res, gate


@pytest.mark.asyncio
async def test_launchpad_settled_notice_comes_after_the_record():
    events = []
    res, gate = await _run_launchpad(events)

    assert "CONFIRMED" in (res.extracted_content or ""), res.error
    assert gate.recorded
    settled = _settled(events)
    assert len(settled) == 1, f"exactly one settled notice, got {events}"
    assert settled[0][2] is True
    assert _index(events, "record") < events.index(settled[0]), (
        f"the settled notice claimed a record before it happened: {events}")


@pytest.mark.asyncio
async def test_launchpad_record_failure_notifies_not_recorded():
    events = []
    with pytest.raises(_Boom):
        await _run_launchpad(events, raises=True)

    settled = _settled(events)
    assert len(settled) == 1, f"a failed record still notifies once: {events}"
    assert settled[0][2] is False, "never a silent True over a failed record"


# ==========================================================================
# The renderer is why the field matters
# ==========================================================================

def test_render_settled_warns_only_on_false():
    base = dict(verb="deploy_token", route="base", tx_ref="0x" + "ab" * 32,
                state=tx_notify.STATE_CONFIRMED)
    assert "NOT recorded" in tx_notify.render_settled(
        tx_notify.TxNotice(**base, ledger_recorded=False))
    assert "NOT recorded" not in tx_notify.render_settled(
        tx_notify.TxNotice(**base, ledger_recorded=True))
