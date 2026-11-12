"""`/nft trust`: the owner pins an agent-NFT collection from chat, never the agent.

* the quote reads the chain and only prints; ``go`` re-reads and writes a profile with NO
  capabilities to the owner's pin store, which ``collection_registry.profiles`` merges;
* an admin (root-file) pin wins over an owner pin of the same collection and only root
  removes it;
* every writer refuses anything but a genuine owner turn; the agent's file tools are denied
  the store;
* an unreadable owner store is UNREADABLE, never "nothing is trusted".
"""
import json
import types

import pytest

from core.wallet import abi, collection_registry as R, collection_trust as ct
from core.wallet.token_trust import owner_seat_ctx

COLL = "0x1111111111111111111111111111111111111111"
#: The ``sale`` key an admin profile with the ``mint`` capability carries (115 W8).
SALE_PIN = {"address": "0x" + "5a" * 20, "runtime_sha256": "00" * 32, "max_price_wei": 99 * 10 ** 15}
TREASURY = "0x2222222222222222222222222222222222222222"
CODE = "0x6080604052" + "5b" * 27
DEPLOYED = 1234
HEAD = 5000


def _word(n: int) -> str:
    return "0x" + hex(n)[2:].rjust(64, "0")


IMPL = "0x" + "a1" * 20
MARKET = "0x" + "b1" * 20
ZONE = "0x" + "c1" * 20


def _addr_word(a: str) -> str:
    return "0x" + a[2:].lower().rjust(64, "0")


def fake_rpc(*, erc721=True, max_supply=6551, archive=True, pruned_empty=False, polyrob=False,
             zone=True, coll_iface=True, acct_iface=True, rails=True):
    sel = {abi.selector("supportsInterface(bytes4)"): _word(1 if erc721 else 0),
           abi.selector("maxSupply()"): _word(max_supply) if max_supply else None,
           abi.selector("MAX_SUPPLY()"): None,
           abi.selector("balanceOf(address)"): _word(2),
           abi.selector("name()"): None, abi.selector("symbol()"): None}
    if polyrob:
        sel.update({abi.selector("accountImplementation()"): _addr_word(IMPL),
                    abi.selector("MARKET()"): _addr_word(MARKET) if rails else _addr_word("0x" + "00" * 20),
                    abi.selector("ZONE()"): (_addr_word(ZONE) if rails else _addr_word("0x" + "00" * 20))
                    if zone else None})
    # ERC-165 by target and id (the detection rule also asks 0x01ffc9a7 and 0xffffffff)
    coll_ids = {"80ac58cd": erc721, "01ffc9a7": True, "6b7481e3": polyrob and coll_iface}
    impl_ids = {"01ffc9a7": True, "92ad233c": acct_iface}

    def _supports(to, data):
        iid = data[10:18]
        ids = impl_ids if str(to).lower() == IMPL.lower() else coll_ids
        return _word(1 if ids.get(iid) else 0)

    def rpc(method, params, timeout=8.0):
        if method == "eth_blockNumber":
            return hex(HEAD)
        if method == "eth_getCode":
            block = params[1]
            if block == "latest":
                return CODE
            if not archive:
                raise RuntimeError("missing trie node")
            if pruned_empty and int(block, 16) < 4000:
                return "0x"  # a pruned node that answers "no code" instead of an error
            return CODE if int(block, 16) >= DEPLOYED else "0x"
        if method == "eth_getBalance":
            if pruned_empty or not archive:
                raise RuntimeError("missing trie node")
            return "0x0"
        if method == "eth_call":
            if params[0]["data"][:10] == abi.selector("supportsInterface(bytes4)"):
                return _supports(params[0]["to"], params[0]["data"])
            got = sel.get(params[0]["data"][:10])
            if got is None:
                raise RuntimeError("execution reverted")
            return got
        raise AssertionError(method)
    return rpc


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    store = str(tmp_path / "wallet" / R.OWNER_PINS_NAME)
    monkeypatch.setattr(R, "owner_pins_path", lambda data_home=None: store)
    monkeypatch.setattr(R, "REGISTRY_FILE", str(tmp_path / "etc" / "agent_nft_collections.json"))
    monkeypatch.setattr(ct, "_treasury", lambda: TREASURY)
    yield store


def _owner():
    return owner_seat_ctx("alice")


def test_quote_reads_only_and_prints_the_typed_confirm_line(_stores):
    text = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(),
                          confirm_line=f"/nft trust {COLL}")
    assert "supply cap: 6551" in text and f"block {DEPLOYED}" in text and "I hold:     2" in text
    assert text.rstrip().endswith(f"/nft trust {COLL} go")
    assert R.profiles() == ()


def test_go_pins_without_capabilities_and_the_registry_reads_it(_stores):
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc())
    assert ok, msg
    (p,) = R.profiles()
    assert p.address == COLL and p.chain_id == 4663 and p.capabilities == ()
    assert p.deploy_block == DEPLOYED and p.max_supply == 6551
    assert R.pinned_with_source()[0][1] == "owner"
    assert R.profiles_on(4663, "mint") == ()  # an owner pin never arms a mint


def test_the_agent_cannot_trust(_stores):
    leaf = types.SimpleNamespace(user_id="alice", role="leaf", is_sub_agent=False, metadata={})
    ok, msg = ct.trust(leaf, "robinhood", COLL, rpc=fake_rpc())
    assert not ok and "denied" in msg and R.profiles() == ()
    stranger = owner_seat_ctx("mallory")
    assert not ct.trust(stranger, "robinhood", COLL, rpc=fake_rpc())[0]


def test_not_an_erc721_refuses(_stores):
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(erc721=False))
    assert not ok and "ERC-721" in msg and R.profiles() == ()


def test_unreadable_supply_and_old_state_name_the_remedy(_stores):
    text = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(max_supply=0))
    assert "max <n>" in text
    text = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(archive=False))
    assert "from <block>" in text
    ok, _ = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(archive=False, max_supply=0),
                     max_supply=100, from_block=7)
    assert ok and R.profiles()[0].deploy_block == 7 and R.profiles()[0].max_supply == 100


def test_admin_pin_wins_and_only_root_removes_it(_stores, tmp_path):
    ok, _ = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc())
    raw = json.load(open(_stores))["profiles"][0]
    admin = tmp_path / "etc" / "agent_nft_collections.json"
    admin.parent.mkdir()
    admin.write_text(json.dumps({"profiles": [dict(raw, capabilities=["mint"], sale=SALE_PIN)]}))
    admin.chmod(0o444)
    rows = R.pinned_with_source()
    assert [s for _p, s in rows] == ["admin"] and rows[0][0].capabilities == ("mint",)
    ok, msg = ct.untrust(_owner(), "robinhood", COLL)
    assert ok  # the owner's own pin goes
    ok, msg = ct.untrust(_owner(), "robinhood", COLL)
    assert not ok and "only root" in msg


def test_untrust_removes_the_owner_pin(_stores):
    ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc())
    ok, msg = ct.untrust(_owner(), "robinhood", COLL)
    assert ok, msg
    assert R.profiles() == ()


def test_an_unreadable_owner_store_is_not_empty(_stores):
    import os
    os.makedirs(os.path.dirname(_stores))
    open(_stores, "w").write("{not json")
    with pytest.raises(R.CollectionRegistryError):
        R.pinned_with_source()
    assert R.profiles() == ()  # the guard: no owner pin, so no act through one
    assert "UNREADABLE" in ct.status_lines()[0]


def test_status_names_the_remedy_when_nothing_is_trusted(_stores):
    assert "/nft trust <collection> on <chain>" in ct.status_lines()[0]


def test_the_agent_file_tools_are_denied_the_store():
    from pathlib import Path

    from core.security.secret_guard import is_credential_file
    assert is_credential_file(Path("/var/lib/polyrob/wallet/collection_pins.json"))
    assert is_credential_file(Path("/var/lib/polyrob/wallet/collection_pins.json.tmp"))


def test_the_chat_seat_quotes_a_card_line_then_pins_on_go(_stores, monkeypatch):
    import asyncio

    from core.surfaces.cards import confirm_line_in
    from surfaces.telegram.nft_ops import nft_reply
    monkeypatch.setattr(ct, "_rpc_for", lambda chain: fake_rpc())
    refused = asyncio.run(nft_reply("alice", ["trust", COLL]))
    assert "Name the network" in refused and "no default network" in refused
    args = ["trust", COLL, "on", "robinhood"]
    quote = asyncio.run(nft_reply("alice", args))
    assert confirm_line_in("/nft", args, quote) == args
    assert R.profiles() == ()
    done = asyncio.run(nft_reply("alice", args + ["go"]))
    assert "Trusted" in done and R.profiles()[0].address == COLL


def test_an_uncapped_supply_is_refused_for_a_chat_pin(_stores):
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(max_supply=2**64))
    assert not ok and "admin" in msg and R.profiles() == ()
    assert "above" in ct.trust_quote("robinhood", COLL, rpc=fake_rpc(), max_supply=10**6)


def _write_store(path, rows):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(json.dumps({"profiles": rows}))


def _row(**over):
    ok, _ = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc())
    import os
    raw = json.load(open(R.owner_pins_path()))["profiles"][0]
    os.remove(R.owner_pins_path())
    return dict(raw, **over)


def test_a_forged_owner_store_cannot_arm_mint_or_a_journal(_stores):
    for bad in ({"capabilities": ["mint"]}, {"capabilities": ["mint"], "sale": SALE_PIN},
                {"sale": SALE_PIN},
                {"journal_log": "0x3333333333333333333333333333333333333333"},
                {"max_supply": 10**9}):
        _write_store(_stores, [_row(**bad)])
        with pytest.raises(R.CollectionRegistryError):
            R.load_owner_pins()
        assert R.profiles() == () and R.profiles_on(4663, "mint") == ()
        assert "UNREADABLE" in ct.status_lines()[0]


def test_a_broken_owner_store_does_not_take_the_admin_pins_down(_stores, tmp_path):
    row = _row()
    admin = tmp_path / "etc" / "agent_nft_collections.json"
    admin.parent.mkdir()
    admin.write_text(json.dumps({"profiles": [dict(row, capabilities=["mint"], sale=SALE_PIN)]}))
    admin.chmod(0o444)
    import os
    os.makedirs(os.path.dirname(_stores), exist_ok=True)
    open(_stores, "w").write("{broken")
    assert [p.address for p in R.profiles()] == [COLL]  # the guard keeps the admin pin
    with pytest.raises(R.CollectionRegistryError):
        R.pinned_with_source()  # the view says unreadable
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc())
    assert not ok and "/nft untrust all go" in msg
    ok, msg = ct.untrust(_owner(), "robinhood", "all")
    assert ok and not os.path.exists(_stores)
    assert any(n.startswith(R.OWNER_PINS_NAME + ".cleared-") for n in os.listdir(os.path.dirname(_stores)))


def test_a_pruned_node_that_answers_empty_code_is_not_trusted_for_the_deploy_block(_stores):
    text = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(pruned_empty=True))
    assert "from <block>" in text


def test_a_from_block_past_the_head_refuses(_stores):
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(), from_block=HEAD + 1)
    assert not ok and "not a block" in msg


# ------------------------------------------------------------------ the collection's own account
def test_a_collection_without_its_own_account_is_pinned_with_account_v3(_stores):
    from core.wallet import erc6551
    f = ct.read_facts("robinhood", COLL, rpc=fake_rpc())
    assert f["profile"]["spec"] == R.SPEC_V2
    assert f["profile"]["accounts"][0] == {"kind": "tokenbound_v3", "registry": erc6551.REGISTRY,
                                           "implementation": erc6551.ACCOUNT_V3_IMPL, "salt": 0}
    assert "standard" not in f["profile"] and "transfer_safe" not in f["profile"]


def test_an_erc6551p_collection_is_pinned_spec2_with_its_account_market_and_zone(_stores):
    import hashlib
    from core.wallet import account_kinds
    sha = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()
    f = ct.read_facts("robinhood", COLL, rpc=fake_rpc(polyrob=True))
    prof = f["profile"]
    assert prof["spec"] == R.SPEC_V2
    assert prof["accounts"][0]["kind"] == "erc6551p"
    assert prof["accounts"][0]["implementation"] == IMPL
    assert prof["accounts"][0]["runtime_sha256"] == sha
    assert prof["standard"] == {"erc6551p": {"market": MARKET, "market_runtime_sha256": sha,
                                             "zone": ZONE, "zone_runtime_sha256": sha}}
    (p,) = R.parse({"profiles": [prof]})
    assert p.accounts[0].kind is account_kinds.ERC6551P
    quote = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(polyrob=True))
    assert "ERC-6551P account" in quote and "ERC-165 proven" in quote and "PolyrobAccount" not in quote
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(polyrob=True))
    assert ok, msg
    assert R.load_owner_pins()[0].accounts[0].kind is account_kinds.ERC6551P


@pytest.mark.parametrize("kw, says", [
    ({"coll_iface": False}, "0x6b7481e3: no"),
    ({"acct_iface": False}, "0x92ad233c: no"),
])
def test_an_implementation_that_erc165_does_not_prove_refuses_to_pin(_stores, kw, says):
    """NA1: accountImplementation() alone is not a proof of the kind — the collection must
    answer IERC6551PCollection and the implementation ITransferSafeAccount by ERC-165."""
    msg = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(polyrob=True, **kw))
    assert msg.startswith("❌") and "ERC-165 does not prove" in msg and says in msg, msg
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(polyrob=True, **kw))
    assert not ok and "Nothing was written" in msg
    assert R.load_owner_pins() == ()


def test_a_contract_that_says_yes_to_every_interface_proves_nothing(_stores):
    def always(method, params, timeout=8.0):
        return "0x" + "00" * 31 + "01"
    assert ct.erc165_proves(always, COLL, "0x6b7481e3") is False  # 0xffffffff answered yes


def test_an_erc6551p_collection_without_sale_rails_pins_without_the_standard_block(_stores):
    f = ct.read_facts("robinhood", COLL, rpc=fake_rpc(polyrob=True, rails=False))
    assert f["profile"]["accounts"][0]["kind"] == "erc6551p" and "standard" not in f["profile"]
    assert "sale rails: none pinned" in ct.trust_quote("robinhood", COLL,
                                                       rpc=fake_rpc(polyrob=True, rails=False))


def test_an_owner_store_written_in_spec_1_keeps_loading(_stores):
    """Existing chat pins on disk (spec 1) load, and a new spec-2 pin is appended beside them."""
    import os
    from core.wallet import account_kinds
    os.makedirs(os.path.dirname(_stores), exist_ok=True)
    old = dict(_row(), address="0x" + "c2" * 20)
    old["spec"] = R.SPEC_V1
    old["accounts"] = [{k: v for k, v in a.items() if k != "kind"} for a in old["accounts"]]
    open(_stores, "w").write(json.dumps({"profiles": [old]}))
    (p,) = R.load_owner_pins()
    assert p.source_spec == R.SPEC_V1 and p.accounts[0].kind is account_kinds.TOKENBOUND_V3
    ok, msg = ct.trust(_owner(), "robinhood", COLL, rpc=fake_rpc(polyrob=True))
    assert ok, msg
    assert [q.source_spec for q in R.load_owner_pins()] == [R.SPEC_V1, R.SPEC_V2]


def test_an_unreadable_zone_refuses_instead_of_falling_back_to_account_v3(_stores):
    msg = ct.trust_quote("robinhood", COLL, rpc=fake_rpc(polyrob=True, zone=False))
    assert msg.startswith("❌") and "ZONE" in msg


def test_a_failed_account_implementation_read_refuses_instead_of_pinning_account_v3(_stores):
    """A timeout on accountImplementation() is not "this collection has no account of its
    own": pinning AccountV3 then made every act through the real account refuse."""
    base = fake_rpc(polyrob=True)

    def flaky(method, params, timeout=8.0):
        if method == "eth_call" and params[0]["data"][:10] == abi.selector(
                "accountImplementation()"):
            raise TimeoutError("read timed out")
        return base(method, params, timeout)
    msg = ct.trust_quote("robinhood", COLL, rpc=flaky)
    assert msg.startswith("❌") and "accountImplementation()" in msg
    with pytest.raises(ValueError, match="could not read"):
        ct.read_facts("robinhood", COLL, rpc=flaky)
