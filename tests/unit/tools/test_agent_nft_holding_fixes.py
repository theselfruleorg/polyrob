"""Proposal 115 W0 — fixes found by reading the ERC-6551 account contracts against the tree.

* bind publishes the ACCOUNT's registration file: ``agentWallet`` is the account on its chain
  (the registry records the same address), never the instance wallet on the default chain;
* an account call is sized from ``eth_estimateGas`` even when the simulation gave no gasUsed;
* the registration file advertises ``/mcp`` only when this instance SERVES it;
* a collection's own revert is named;
* the view never says an owner can act through a sale-ready account.
"""
import base64
import json

from core.wallet import abi, erc6551
from core.wallet.broadcast.evm import is_account_call
from core.wallet.tx_guard import Decision
from tests.unit.tools.test_agent_nft_core_verbs import ACCOUNT, ViewChain, _no_package, armed  # noqa: F401
from tests.unit.tools.test_agent_nft_withdraw import FakeRail, _run, _tool
from tools.agent_nft.tool import BindParams


def _doc(uri: str) -> dict:
    assert uri.startswith("data:application/json;base64,")
    return json.loads(base64.b64decode(uri.split(",", 1)[1]))


def test_the_account_file_names_the_account_as_the_agent_wallet(monkeypatch, tmp_path):
    from tools.defi.agent_registration import account_agent_uri
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EIP8004_AGENT_WALLET", "0x" + "ee" * 20)
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example.test")   # public: still a data: URI
    doc = _doc(account_agent_uri(4663, ACCOUNT))
    wallets = [s["endpoint"] for s in doc["services"] if s["name"] == "agentWallet"]
    assert wallets == [f"eip155:4663:{ACCOUNT}"]
    assert doc["registrations"] == []


def test_bind_registers_the_account_file(armed):  # noqa: F811
    tool, seen = _tool(armed, ViewChain(armed.address))
    res = _run(_no_package(tool).agent_nft_bind_identity(
        BindParams(chain="robinhood", nft="3", dry_run=True)))
    assert res.error is None, res.error
    (_intent, tx, _), = seen
    inner = erc6551.decode_execute(tx["data"])[2]
    assert inner.startswith(abi.selector("register(string)"))
    (uri,) = abi.decode([{"type": "string"}], "0x" + inner[10:])
    wallets = [s["endpoint"] for s in _doc(uri)["services"] if s["name"] == "agentWallet"]
    assert wallets == [f"eip155:4663:{ACCOUNT}"]


def test_an_account_call_is_sized_without_a_simulation_measurement(armed):  # noqa: F811
    sized = []

    class Rail(FakeRail):
        def size_gas(self, tx, used):
            sized.append(used)
            return {**tx, "gas": 333_333}

    unmeasured = Decision(True, "authorized", lane="owner_direct", amount_usd=0.01, sim_gas_used=None)
    tool, _seen = _tool(armed, ViewChain(armed.address), decision=unmeasured)
    tool._rail_factory = Rail
    res = _run(_no_package(tool).agent_nft_bind_identity(
        BindParams(chain="robinhood", nft="3", dry_run=True)))
    assert res.error is None, res.error
    assert sized == [None], "the account call kept the built default gas limit"


def test_is_account_call_reads_the_two_account_selectors():
    assert is_account_call({"data": "0x51945447" + "00" * 4})
    assert is_account_call({"data": "0x43629A73"})
    assert not is_account_call({"data": "0xa9059cbb"})
    assert not is_account_call({})


def test_mcp_is_advertised_by_the_serve_flag_only(monkeypatch, tmp_path):
    from modules.eip8004.registration import build_registration_file
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OVERRIDES", "MCP_ENABLED=true")
    monkeypatch.delenv("MCP_SERVE_ENABLED", raising=False)
    names = [s.name for s in build_registration_file("https://agent.example.test").services]
    assert "MCP" not in names, "the client flag serves nothing"
    monkeypatch.setenv("MCP_SERVE_ENABLED", "true")
    names = [s.name for s in build_registration_file("https://agent.example.test").services]
    assert "MCP" in names


def test_a_collection_revert_is_named():
    for sel, word in (("0x620e721c", "SaleNotReady"), ("0xd15af9b5", "AccountInBatch"),
                      ("0xc74ed4fa", "SellerIsAccount"), ("0x68da804f", "NotTokenAccount"),
                      ("0x59dc379f", "NotTokenOwner"), ("0xb3077bb1", "NotTicketOpener"),
                      ("0xd9e1a63a", "TicketBusy")):
        assert abi.selector(word + "()") == sel
        from core.wallet import collection_errors, custom_errors
        assert custom_errors.decode(sel, collection_errors.COLLECTION_ERRORS).startswith(word)


def test_the_view_says_an_act_ends_readiness():
    import inspect
    from tools.agent_nft import view
    text = inspect.getsource(view)
    assert "can still act through the account" not in text
    assert "ENDS readiness" in text
