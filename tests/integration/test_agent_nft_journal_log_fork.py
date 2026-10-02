"""J1 + J3 on a FORK of Robinhood Chain 4663: the journal entry rides the action's executeBatch.

Opt-in: ``AGENT_NFT_FORK_E2E=1`` and a FRESH anvil fork at ``AGENT_NFT_FORK_RPC`` (default
``http://127.0.0.1:8550``)::

    anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8550

Nothing is broadcast to a real chain: every transaction is ``eth_sendTransaction`` from an
impersonated address on the fork, which is snapshotted and reverted. The entry signatures come
from FRESH keys made per run (never a well-known dev key). ``JournalLog`` is the polyrob-desk
build (``contracts/src/kit/JournalLog.sol`` at a701720, solc 0.8.24), deployed on the fork.

1. The NFT's owner sends 0.001 ETH from the account with its signed entry as the batch's last
   leg; the guard allows it, the receipt carries ``Entry(account, entry)`` and the event's bytes
   decode to the signed entry.
2. The NFT is sold; the new owner's first entry links to the seller's last one (J3), read from
   the chain, and both verify.
"""
import json
import os
import time
import urllib.request

import pytest
from eth_account import Account

from core.wallet import abi, collection_registry, erc6551, journal_log, nft_account, tx_guard
from core.wallet.erc8004 import _IDENTITY_MAINNET
from core.wallet.policy import PolicyGate
from core.wallet.signer import LocalEoaSigner
from tools.defi import account_mode

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_NFT_FORK_E2E") != "1",
                                reason="fork e2e: set AGENT_NFT_FORK_E2E=1 and run anvil (see module doc)")

RPC = os.environ.get("AGENT_NFT_FORK_RPC", "http://127.0.0.1:8550")
DEST = "0x000000000000000000000000000000000000bEEF"
#: polyrob-desk JournalLog creation code (a701720, solc 0.8.24).
JOURNAL_LOG_INIT = "0x608060405234801561000f575f80fd5b506101598061001d5f395ff3fe608060405234801561000f575f80fd5b5060043610610029575f3560e01c80630be77f561461002d575b5f80fd5b61004061003b366004610089565b610042565b005b336001600160a01b03167fea73436bac7c599269868df7096541c0c17491cbda227e7a8aa5bb075658fb39838360405161007d9291906100f5565b60405180910390a25050565b5f806020838503121561009a575f80fd5b823567ffffffffffffffff808211156100b1575f80fd5b818501915085601f8301126100c4575f80fd5b8135818111156100d2575f80fd5b8660208285010111156100e3575f80fd5b60209290920196919550909350505050565b60208152816020820152818360408301375f818301604090810191909152601f909201601f1916010191905056fea26469706673582212207f4fb118aa2af624b63bd43421f4e16c05a498b63fe454294c1efe82033209e264736f6c63430008180033"  # noqa: E501


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _send(frm, to, data, value=0):
    _rpc("anvil_impersonateAccount", [frm])
    tx = {"from": frm, "data": data, "value": hex(value), "gas": hex(3_000_000)}
    if to:
        tx["to"] = to
    h = _rpc("eth_sendTransaction", [tx])
    for _ in range(50):
        rc = _rpc("eth_getTransactionReceipt", [h])
        if rc:
            break
        time.sleep(0.1)
    assert rc and int(rc["status"], 16) == 1, rc
    return rc


def _fresh():
    return LocalEoaSigner(bytes(Account.create().key))


class _Rail:
    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value, "chainId": 4663, "nonce": 0,
                "gas": 500_000, "maxFeePerGas": 10 ** 9}


@pytest.fixture
def fork(monkeypatch, tmp_path):
    try:
        if int(_rpc("eth_chainId", []), 16) != 4663:
            pytest.skip(f"{RPC} is not a 4663 fork")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no fork at {RPC}: {exc}")
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", RPC)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    snap = _rpc("evm_snapshot", [])
    parent, token_id = _IDENTITY_MAINNET, 1
    owner = abi.decode([{"type": "address"}], _rpc("eth_call", [{"to": parent, "data": abi.encode_call(
        "ownerOf", [{"type": "uint256"}], [token_id])}, "latest"]))[0]
    seller = _fresh()
    for a in (owner, seller.address):
        _rpc("anvil_setBalance", [a, hex(10 ** 18)])
    _send(owner, erc6551.REGISTRY, erc6551.encode_create_account(4663, parent, token_id))
    account = erc6551.account_address(4663, parent, token_id)
    _send(owner, parent, abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [owner, seller.address, token_id]))
    _rpc("anvil_setBalance", [account, hex(10 ** 18)])
    jlog = _send(seller.address, None, JOURNAL_LOG_INIT)["contractAddress"]
    block = int(_rpc("eth_blockNumber", []), 16)
    pin = collection_registry.CollectionProfile(
        spec=collection_registry.SPEC_V1, capabilities=(), chain_id=4663, address=parent.lower(),
        runtime_sha256=collection_registry.runtime_sha256_of(_rpc, parent), deploy_block=block,
        max_supply=token_id, journal_log=jlog.lower(), accounts=(collection_registry.AccountVersion(
            erc6551.REGISTRY.lower(), erc6551.ACCOUNT_V3_IMPL.lower(), erc6551.ACCOUNT_SALT),))
    monkeypatch.setattr(collection_registry, "profiles", lambda: (pin,))
    yield {"account": account, "parent": parent, "token_id": token_id, "seller": seller,
           "jlog": jlog}
    _rpc("evm_revert", [snap])


def _act(fork, signer, text):
    """Build, authorize and send one journaled 0.001 ETH send from the account as *signer*."""
    held = nft_account.resolve("robinhood", rpc=_rpc, treasury=signer.address,
                               nft=f"{fork['parent']}#{fork['token_id']}")
    assert held.journal_log == fork["jlog"].lower()
    entry, why = account_mode.prepare_journal(held, signer, kind="tend", text=text, rpc=_rpc)
    assert entry is not None, why
    tx, state = account_mode.wrap(_Rail(), {"to": DEST, "value": 10 ** 15, "data": "0x"}, held,
                                  _rpc, journal=entry)
    intent = account_mode.intent_for(tx_guard.TxIntent(
        chain="robinhood", token=None, to=DEST, amount_raw=10 ** 15, max_spend_usd=20.0,
        idempotency_key=f"j1-{entry['seq']}"), held, state, journal=True)
    d = tx_guard.authorize(
        intent, tx, holder=signer.address,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0), execution_context=None,
        price_fn=lambda chain, addr: 4000.0, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False,
        account_rpc=_rpc)
    assert d.allowed is True, d.reason
    rc = _send(signer.address, held.account, tx["data"])
    return held, entry, rc


def test_j1_the_action_and_its_entry_land_in_one_transaction(fork):
    before = int(_rpc("eth_getBalance", [DEST, "latest"]), 16)
    held, entry, rc = _act(fork, fork["seller"], "send 0.001 ETH to DEST")
    assert int(_rpc("eth_getBalance", [DEST, "latest"]), 16) - before == 10 ** 15
    logs = [lg for lg in rc["logs"] if lg["topics"][0] == journal_log.TOPIC_ENTRY]
    assert len(logs) == 1 and logs[0]["address"].lower() == fork["jlog"].lower()
    assert logs[0]["topics"][1][-40:] == held.account.lower()[2:]
    (on_chain,) = journal_log.read_entries(_rpc, fork["jlog"], held.account, held.deploy_block)
    assert on_chain == entry
    assert nft_account.recover_owner(on_chain) == fork["seller"].address.lower()
    print(f"J1 fork: tx gasUsed={int(rc['gasUsed'], 16)} entry={len(nft_account.canonical(entry))} bytes")


def test_j3_the_buyers_first_entry_links_to_the_sellers_last(fork):
    _held, first, _rc = _act(fork, fork["seller"], "before the sale")
    buyer = _fresh()
    _rpc("anvil_setBalance", [buyer.address, hex(10 ** 18)])
    _send(fork["seller"].address, fork["parent"], abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [fork["seller"].address, buyer.address, fork["token_id"]]))
    held, second, _rc = _act(fork, buyer, "after the sale")
    assert second["seq"] == 1 and second["prev"] == nft_account.digest(first)
    chain = nft_account.verified_chain(
        journal_log.read_entries(_rpc, fork["jlog"], held.account, held.deploy_block),
        account=held.account, chain_id=4663)
    assert chain == [first, second]
    assert [nft_account.recover_owner(e) for e in chain] == [
        fork["seller"].address.lower(), buyer.address.lower()]


def test_j1_the_seller_cannot_write_after_the_sale(fork):
    """Only the NFT's current owner can make the account call JournalLog: the guard refuses the
    old owner (pre-flight), and so does the account itself."""
    buyer = _fresh()
    _send(fork["seller"].address, fork["parent"], abi.encode_call(
        "transferFrom", [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
        [fork["seller"].address, buyer.address, fork["token_id"]]))
    with pytest.raises(Exception):
        nft_account.resolve("robinhood", rpc=_rpc, treasury=fork["seller"].address,
                            nft=f"{fork['parent']}#{fork['token_id']}")
    leg = (fork["jlog"], 0, journal_log.encode_log(b"{}"), 0)
    _rpc("anvil_impersonateAccount", [fork["seller"].address])
    with pytest.raises(RuntimeError):
        _rpc("eth_call", [{"from": fork["seller"].address, "to": fork["account"],
                           "data": erc6551.encode_execute(*leg)}, "latest"])


def test_j1_a_note_with_no_action_is_the_accounts_single_call(fork):
    """``is_journal_entry``: the account's only call is JournalLog.log(entry); nothing moves."""
    signer = fork["seller"]
    held = nft_account.resolve("robinhood", rpc=_rpc, treasury=signer.address,
                               nft=f"{fork['parent']}#{fork['token_id']}")
    entry, why = account_mode.prepare_journal(held, signer, kind="note", text="a thesis", rpc=_rpc)
    assert entry is not None, why
    state = erc6551.read_state(_rpc, held.account)
    data = erc6551.encode_execute(held.journal_log, 0,
                                  journal_log.encode_log(nft_account.canonical(entry)), 0)
    tx = dict(_Rail().build_call(to=held.account, data=data), value=0)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=held.journal_log, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="note", via_account=held.account,
                               via_account_state=state, is_journal_entry=True)
    d = tx_guard.authorize(
        intent, tx, holder=signer.address,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0), execution_context=None,
        price_fn=lambda chain, addr: 4000.0, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False,
        account_rpc=_rpc)
    assert d.allowed is True, d.reason
    rc = _send(signer.address, held.account, data)
    (on_chain,) = journal_log.read_entries(_rpc, fork["jlog"], held.account, held.deploy_block)
    assert on_chain == entry
    print(f"J1 note fork: gasUsed={int(rc['gasUsed'], 16)}")
