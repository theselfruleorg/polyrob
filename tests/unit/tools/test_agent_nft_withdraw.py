"""069 v4 A4 — the core-owned ``agent_nft_withdraw_token`` (``/nft send <id> <to>``) and
``agent_nft_revoke_all``. The guard's own rules (nesting, rule 5, code pin) are pinned in
tests/unit/core/wallet/test_tx_guard_pinned_nft_departure.py and test_tx_guard_nft_nesting.py;
here: the verb builds the right intent, names the NFT, needs no package, and refuses early."""
import asyncio
import os

import pytest

from core.wallet import abi, erc6551, nft_holdings
from core.wallet.broadcast.evm import Receipt
from core.wallet.policy import PolicyGate
from core.wallet.signer import LocalEoaSigner
from core.wallet.tx_guard import Decision
from tests.collection_pins import CODE, pin, profile
from tools.agent_nft.tool import AgentNftTool, RevokeAllParams, TakeParams

PINNED = "0xC011000000000000000000000000000000000001"
STRANGER = "0x" + "77" * 20
TO = "0x" + "3c" * 20
SPENDER = "0x" + "5e" * 20
TOKEN = "0x" + "a0" * 20
ACCOUNT = erc6551.account_address(4663, PINNED, 3)
SEL = {k: abi.selector(k) for k in ("ownerOf(uint256)", "state()", "allowance(address,address)")}


def _w(v):
    return "0x" + "0" * 24 + v[2:].lower() if isinstance(v, str) else "0x" + f"{int(v):064x}"


class Chain:
    def __init__(self, owner, approvals=()):
        self.owner, self.approvals = owner, list(approvals)

    def __call__(self, method, params, *a, **k):
        if method == "eth_getCode":
            return CODE if params[0].lower() == PINNED.lower() else "0x" + "60" * 45
        if method == "eth_blockNumber":
            return hex(0x300)
        if method == "eth_getLogs":
            return [{"address": TOKEN, "blockNumber": hex(0x10), "logIndex": "0x0",
                     "topics": [erc6551.TOPIC_APPROVAL, _w(ACCOUNT), _w(s)], "data": _w(7)}
                    for s in self.approvals]
        if method == "eth_call":
            sel = params[0]["data"][:10]
            if sel == SEL["ownerOf(uint256)"]:
                return _w(self.owner)
            if sel == SEL["state()"]:
                return _w(11)
            if sel == SEL["allowance(address,address)"]:
                return _w(7)
        raise AssertionError(f"{method} {params}")


class FakeRail:
    sent = []

    def __init__(self, chain, signer):
        pass

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "chainId": 4663, "gas": 200_000}

    def size_gas(self, tx, used):
        return tx

    def sign_and_send(self, tx):
        FakeRail.sent.append(tx)
        return "0x" + "ef" * 32

    def await_receipt(self, tx_hash):
        return Receipt(tx_hash=tx_hash, status="success", block_number=9, gas_used=80_000)


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    FakeRail.sent = []
    return LocalEoaSigner(os.urandom(32))


def _tool(signer, chain, decision=None):
    seen = []

    def guard(intent, tx, **kw):
        seen.append((intent, tx, kw))
        return decision or Decision(True, "authorized", lane="owner_direct", amount_usd=0.01,
                                    sim_gas_used=90_000)
    wallet = type("W", (), {"operational_signer": lambda self: signer,
                            "policy": PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0)})()
    return AgentNftTool(wallet=wallet, guard_fn=guard, rpc_fn=chain, rail_factory=FakeRail), seen


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_withdraw_needs_a_destination():
    with pytest.raises(Exception):
        TakeParams()
    with pytest.raises(Exception):
        TakeParams(to="not-an-address")


def test_withdraw_is_a_treasury_transfer_declared_as_nft_out_and_needs_no_package(armed):
    tool, seen = _tool(armed, Chain(armed.address))
    tool._impl = lambda: None                                     # package absent
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=TO, nft="3", dry_run=False)))
    assert res.error is None, res.error
    intent, tx, kw = seen[0]
    assert intent.is_nft_op and intent.nft_out == ((PINNED.lower(), "erc721", 3, 1),)
    assert intent.via_account is None and tx["to"].lower() == PINNED.lower()
    assert tx["data"] == abi.encode_call(
        "safeTransferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [armed.address, TO, 3])
    assert "journal: entry #0 (handover) signed" in res.extracted_content
    assert FakeRail.sent


def test_withdraw_defaults_to_the_only_tracked_nft(armed):
    tool, seen = _tool(armed, Chain(armed.address))
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=TO)))
    assert "holds no tracked NFT" in res.error and seen == []
    path = nft_holdings.state_path()
    state = nft_holdings.load_state(path)
    state["tracked"][f"4663:{PINNED.lower()}:3"] = {
        "chain": "robinhood", "chain_id": 4663, "collection": PINNED.lower(), "token_id": 3,
        "account": ACCOUNT, "label": "POLYROB #3", "since": 1.0}
    nft_holdings.save_state(path, state)
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=TO)))
    assert res.error is None and seen[0][0].nft_out[0][2] == 3


def test_withdraw_refuses_an_nft_this_treasury_does_not_own(armed):
    tool, seen = _tool(armed, Chain(STRANGER))
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=TO, nft="3")))
    assert "does not own" in res.error and seen == []


def test_withdraw_to_the_treasury_itself_refuses(armed):
    tool, seen = _tool(armed, Chain(armed.address))
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=armed.address, nft="3")))
    assert "treasury itself" in res.error and seen == []


def test_a_refused_guard_sends_nothing_and_journals_nothing(armed):
    tool, seen = _tool(armed, Chain(armed.address),
                       decision=Decision(False, "refused: 1 open approval(s) … agent_nft_revoke_all"))
    res = _run(tool.agent_nft_withdraw_token(TakeParams(to=TO, nft="3", dry_run=False)))
    assert "NOT SENT" in res.extracted_content and "agent_nft_revoke_all" in res.extracted_content
    assert "journal" not in res.extracted_content and not FakeRail.sent


def test_revoke_all_revokes_each_open_approval_through_the_account(armed):
    tool, seen = _tool(armed, Chain(armed.address, approvals=[SPENDER]))
    tool._impl = lambda: None
    res = _run(tool.agent_nft_revoke_all(RevokeAllParams(nft="3", dry_run=False)))
    assert res.error is None, res.error
    intent, tx, kw = seen[0]
    assert intent.via_account == ACCOUNT and intent.via_account_state == 11
    assert intent.is_allowance_op and intent.token.lower() == TOKEN.lower()
    to, value, data, op = erc6551.decode_execute(tx["data"])
    assert to.lower() == TOKEN.lower() and value == 0 and op == 0
    assert data == abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}], [SPENDER, 0])
    assert "journal: entry #0 (tend) signed" in res.extracted_content


def test_revoke_all_with_nothing_open_says_the_table_is_complete(armed):
    tool, seen = _tool(armed, Chain(armed.address))
    res = _run(tool.agent_nft_revoke_all(RevokeAllParams(nft="3")))
    assert "no open approvals" in res.extracted_content and "(complete)" in res.extracted_content
    assert seen == []


def test_nft_send_on_the_owner_seat_runs_the_withdraw(armed, monkeypatch):
    from surfaces.telegram import nft_ops
    got = {}

    async def fake(self, params, ctx=None):
        got["params"] = params
        return self._ar(content="SEND ok")
    monkeypatch.setattr(AgentNftTool, "agent_nft_withdraw_token", fake)
    out = _run(nft_ops.nft_reply("owner", ["send", "3", TO]))
    assert got["params"].nft == "3" and got["params"].to == TO and got["params"].dry_run is True
    assert got["params"].chain == "robinhood" and "Add `go`" in out
    _run(nft_ops.nft_reply("owner", ["send", "3", TO, "go"]))
    assert got["params"].dry_run is False


def test_c17_revoke_all_revokes_erc6909_allowances_and_operators(armed, monkeypatch):
    rows = [erc6551.OpenApproval("erc6909", PINNED, SPENDER, 7, 300, 5, True),
            erc6551.OpenApproval("erc6909_operator", PINNED, SPENDER, None, None, 6, True)]
    monkeypatch.setattr(erc6551, "open_approvals", lambda *a, **k: list(rows))
    tool, seen = _tool(armed, Chain(armed.address))
    tool._impl = lambda: None
    res = _run(tool.agent_nft_revoke_all(RevokeAllParams(nft="3", dry_run=True)))
    assert res.error is None, res.error
    (i1, tx1, _), (i2, tx2, _) = seen
    assert i1.is_nft_op and i1.erc6909_revokes == ((PINNED, SPENDER, 7),)
    assert erc6551.decode_execute(tx1["data"])[2] == erc6551.encode_erc6909_revoke(SPENDER, 7)
    assert i2.is_nft_op and i2.nft_operator_ops == ((PINNED, SPENDER, False),)
    assert erc6551.decode_execute(tx2["data"])[2] == erc6551.encode_erc6909_operator_revoke(SPENDER)
    assert "unknown kind" not in res.extracted_content
