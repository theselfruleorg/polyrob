"""069 v4 A2 — the owner-pinned collection registry.

Empty by default; a file the service can write, an unreadable file or an off-schema one is
"cannot be trusted" (never "nothing pinned"); an unknown spec, capability, account version or
call shape refuses the whole file.
"""
import copy
import json
import os

import pytest

from core.wallet import account_journal, account_kinds as AK, collection_registry as CR, erc6551
from tests.collection_pins import (MARKET, MAX_PRICE_WEI, POLYROB_IMPL, RUNTIME_SHA256, pin,
                                   polyrob_profile, profile, sale, sale_address, transfer_safe)

ADDR = "0xC011000000000000000000000000000000000001"


def _doc(**over):
    p = profile(ADDR)
    p.update(over)
    return {"profiles": [p]}


def _write(tmp_path, doc, *, mode=0o444):
    path = tmp_path / "agent_nft_collections.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    os.chmod(path, mode)
    return str(path)


def test_the_file_is_owner_territory():
    assert CR.REGISTRY_FILE == "/etc/polyrob/agent_nft_collections.json"


def test_no_file_pins_nothing(tmp_path):
    assert CR.load(str(tmp_path / "absent.json")) == ()


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0, reason="root may read a writable file")
def test_a_read_only_file_loads(tmp_path):
    (p,) = CR.load(_write(tmp_path, _doc(journal_prefix="POLYROB")))
    assert (p.chain_id, p.address, p.max_supply, p.capabilities) == (4663, ADDR.lower(), 6551,
                                                                     ("mint", "reveal"))
    assert p.accounts[0].implementation == erc6551.ACCOUNT_V3_IMPL.lower()
    assert p.journal_prefix == "POLYROB"


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0, reason="root may read a writable file")
def test_a_file_this_process_can_write_is_refused(tmp_path):
    with pytest.raises(CR.CollectionRegistryError, match="writable"):
        CR.load(_write(tmp_path, _doc(), mode=0o644))


def test_an_unreadable_file_is_not_an_empty_one(tmp_path):
    with pytest.raises(CR.CollectionRegistryError):
        CR.load(_write(tmp_path, "{not json"))


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["profiles"][0].update(spec="agent-nft-profile/3"), "spec"),
    (lambda d: d["profiles"][0].update(capabilities=["mint", "airdrop"]), "capability"),
    (lambda d: d["profiles"][0].update(call_shapes={"mint": "mint(address,uint256)"}), "call shape"),
    (lambda d: d["profiles"][0].update(call_shapes={"reveal": "reveal(uint256[])"},
                                       capabilities=["mint"]), "not one of its capabilities"),
    (lambda d: d["profiles"][0]["accounts"][0].update(implementation="0x" + "ab" * 20),
     "account version"),
    (lambda d: d["profiles"][0].update(accounts=[]), "at least one"),
    (lambda d: d["profiles"][0].update(max_supply=0), "max_supply"),
    (lambda d: d["profiles"][0].update(address="0x1234"), "address"),
    (lambda d: d["profiles"][0].update(runtime_sha256="xyz"), "runtime_sha256"),
    (lambda d: d["profiles"][0].update(journal_prefix="bad\nprefix"), "journal_prefix"),
    (lambda d: d["profiles"][0].update(journal_log="0x1234"), "journal_log"),
    (lambda d: d["profiles"][0].update(journal_log=None), "journal_log"),
    (lambda d: d["profiles"][0].update(extra=1), "unknown field"),
    (lambda d: d["profiles"][0].pop("max_supply"), "missing"),
    (lambda d: d["profiles"].append(copy.deepcopy(d["profiles"][0])), "pinned twice"),
    (lambda d: d.update(version=1), "exactly"),
])
def test_off_schema_refuses_the_whole_registry(mutate, needle):
    doc = _doc()
    mutate(doc)
    with pytest.raises(CR.CollectionRegistryError, match=needle):
        CR.parse(doc)


def test_j2_the_journal_log_is_optional():
    """J2: polyrob-desk's ``script/pin.py`` writes ``journal_log``; a profile without it stays valid."""
    (without,) = CR.parse(_doc())
    assert without.journal_log is None
    log = "0x" + "Ab" * 20
    (with_log,) = CR.parse(_doc(journal_log=log))
    assert with_log.journal_log == log.lower()


def test_known_call_shapes_are_accepted():
    (p,) = CR.parse(_doc(call_shapes={"mint": "mint(address,uint256,uint256)",
                                      "reveal": "reveal(uint256[])"}))
    assert p.capabilities == ("mint", "reveal")


def test_lookup_by_chain_and_capability(monkeypatch):
    pin(monkeypatch, profile(ADDR, capabilities=("reveal",)),
        profile("0xC011000000000000000000000000000000000002", chain_id=46630))
    assert [p.address for p in CR.profiles_on(4663)] == [ADDR.lower()]
    assert CR.profiles_on(4663, "mint") == ()
    assert CR.profile_for(4663, ADDR.upper().replace("0X", "0x")).address == ADDR.lower()
    assert CR.profile_for(46630, ADDR) is None


def test_a_pinned_journal_prefix_is_signable(monkeypatch):
    pin(monkeypatch)
    assert account_journal.allowed_journal_prefixes() == ("agent",)
    pin(monkeypatch, profile(ADDR, journal_prefix="POLYROB"))
    assert account_journal.allowed_journal_prefixes() == ("agent", "POLYROB")
    payload = account_journal.journal_payload(account=ADDR, chain=4663, seq=0, kind="note",
                                              text_sha256="ab" * 32, prev="genesis", prefix="POLYROB")
    assert payload.startswith(b"POLYROB account journal\n")


def test_an_untrusted_registry_leaves_only_the_default_prefix(monkeypatch):
    def boom():
        raise CR.CollectionRegistryError("writable")
    monkeypatch.setattr(CR, "profiles", boom)
    assert account_journal.allowed_journal_prefixes() == ("agent",)


# ------------------------------------------------------------------ spec 1 transfer_safe → erc6551p
def test_without_transfer_safe_only_account_v3_is_accepted():
    (p,) = CR.parse(_doc())
    assert p.erc6551p is None
    assert p.accounts[0].kind is AK.TOKENBOUND_V3
    assert p.accounts[0].runtime_sha256 is None


def test_with_transfer_safe_accounts0_is_the_pinned_erc6551p_account():
    (p,) = CR.parse({"profiles": [polyrob_profile(ADDR, journal_log="0x" + "ee" * 20)]})
    v = p.accounts[0]
    assert (v.implementation, v.kind, v.runtime_sha256) == (POLYROB_IMPL, AK.ERC6551P, RUNTIME_SHA256)
    assert p.account_version(POLYROB_IMPL.upper().replace("0X", "0x")) == v
    assert p.erc6551p.market == MARKET and p.erc6551p.zone_runtime_sha256 == RUNTIME_SHA256
    assert p.account_version(erc6551.ACCOUNT_V3_IMPL) is None


def test_a_later_account_version_beside_an_erc6551p_account_must_be_account_v3():
    raw = polyrob_profile(ADDR)
    raw["accounts"].append({"registry": erc6551.REGISTRY, "implementation": erc6551.ACCOUNT_V3_IMPL,
                            "salt": 0})
    (p,) = CR.parse({"profiles": [raw]})
    assert [v.kind for v in p.accounts] == [AK.ERC6551P, AK.TOKENBOUND_V3]
    raw["accounts"][1]["implementation"] = "0x" + "a2" * 20
    with pytest.raises(CR.CollectionRegistryError, match="account version"):
        CR.parse({"profiles": [raw]})


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r["accounts"][0].update(implementation=erc6551.ACCOUNT_V3_IMPL), "is AccountV3"),
    (lambda r: r["accounts"][0].update(registry="0x" + "12" * 20), "registry"),
    (lambda r: r["transfer_safe"].pop("zone"), "exactly"),
    (lambda r: r["transfer_safe"].update(extra=1), "exactly"),
    (lambda r: r["transfer_safe"].update(market="0x12"), "market"),
    (lambda r: r["transfer_safe"].update(account_impl_runtime_sha256="0x" + "11" * 32),
     "account_impl_runtime_sha256"),
    (lambda r: r.update(transfer_safe=None), "exactly"),
])
def test_an_off_schema_transfer_safe_refuses_the_registry(mutate, needle):
    raw = polyrob_profile(ADDR)
    mutate(raw)
    with pytest.raises(CR.CollectionRegistryError, match=needle):
        CR.parse({"profiles": [raw]})


def test_an_unknown_implementation_without_transfer_safe_is_refused():
    raw = profile(ADDR)
    raw["accounts"][0]["implementation"] = POLYROB_IMPL
    with pytest.raises(CR.CollectionRegistryError, match="AccountV3"):
        CR.parse({"profiles": [raw]})


def test_the_desk_pin_writer_profile_is_accepted():
    """The exact shape ``polyrob-desk/contracts/script/pin.py::profile_of(..., transfer_safe=…)`` writes."""
    raw = {"spec": "agent-nft-profile/1", "capabilities": ["reveal"], "chain_id": 4663, "address": ADDR.lower(),
           "runtime_sha256": "11" * 32, "deploy_block": 123, "max_supply": 6551,
           "accounts": [{"registry": erc6551.REGISTRY, "implementation": POLYROB_IMPL, "salt": 0}],
           "journal_prefix": "POLYROB", "call_shapes": {"reveal": "reveal(uint256[])"},
           "journal_log": "0x" + "ee" * 20, "transfer_safe": transfer_safe()}
    (p,) = CR.parse({"profiles": [raw]})
    assert p.accounts[0].kind is AK.ERC6551P and p.journal_log == "0x" + "ee" * 20


# ------------------------------------------------------------------ the sale (115 W8, M5)
SALE = sale_address(ADDR)


def test_a_profile_with_the_mint_capability_carries_its_sale():
    (p,) = CR.parse(_doc())
    assert p.sale == CR.Sale(SALE.lower(), RUNTIME_SHA256, MAX_PRICE_WEI)


def test_a_reveal_only_profile_without_a_sale_is_what_it_was():
    (p,) = CR.parse({"profiles": [profile(ADDR, capabilities=("reveal",))]})
    assert p.sale is None and p.capabilities == ("reveal",)


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r.pop("sale"), "mint capability without a sale"),
    (lambda r: r.update(capabilities=["reveal"]), "sale without the mint capability"),
    (lambda r: r["sale"].update(extra=1), "exactly"),
    (lambda r: r["sale"].pop("max_price_wei"), "exactly"),
    (lambda r: r["sale"].pop("runtime_sha256"), "exactly"),
    (lambda r: r["sale"].pop("address"), "exactly"),
    (lambda r: r.update(sale=None), "exactly"),
    (lambda r: r.update(sale=[]), "exactly"),
    (lambda r: r["sale"].update(address="0x12"), "sale.address"),
    (lambda r: r["sale"].update(runtime_sha256="0x" + "11" * 32), "sale.runtime_sha256"),
    (lambda r: r["sale"].update(runtime_sha256="AB" * 32), "sale.runtime_sha256"),
    (lambda r: r["sale"].update(max_price_wei=0), "max_price_wei"),
    (lambda r: r["sale"].update(max_price_wei=-1), "max_price_wei"),
    (lambda r: r["sale"].update(max_price_wei=True), "max_price_wei"),
    (lambda r: r["sale"].update(max_price_wei="99"), "max_price_wei"),
    (lambda r: r["sale"].update(max_price_wei=9.9e16), "max_price_wei"),
    (lambda r: r["sale"].update(address=ADDR), "also a pinned collection"),
    (lambda r: r.update(call_shapes={"mint_wl": "mintWL(address,uint256)"}), "call shape"),
    (lambda r: r.update(call_shapes={"settle": "settle(uint256[])"}), "unknown"),
    (lambda r: r.update(capabilities=["mint_wl", "mint"]), "capability"),
])
def test_an_off_schema_sale_refuses_the_registry(mutate, needle):
    raw = profile(ADDR)
    mutate(raw)
    with pytest.raises(CR.CollectionRegistryError, match=needle):
        CR.parse({"profiles": [raw]})


def test_the_mint_wl_call_shape_belongs_to_the_mint_capability():
    assert CR.KNOWN_CALL_SHAPES["mint_wl"] == "mintWL(address,uint256,uint256,uint256,bytes32[])"
    raw = profile(ADDR)
    raw["call_shapes"] = {"mint": CR.KNOWN_CALL_SHAPES["mint"],
                          "mint_wl": CR.KNOWN_CALL_SHAPES["mint_wl"]}
    assert CR.parse({"profiles": [raw]})[0].sale.address == SALE.lower()
    raw = profile(ADDR, capabilities=("reveal",))
    raw["call_shapes"] = {"mint_wl": CR.KNOWN_CALL_SHAPES["mint_wl"]}
    with pytest.raises(CR.CollectionRegistryError, match="not one of its capabilities"):
        CR.parse({"profiles": [raw]})


def test_the_known_call_shapes_match_the_mint_module():
    from core.wallet import collection_mint, collection_reveal
    assert CR.KNOWN_CALL_SHAPES == {"mint": collection_mint.MINT_SIGNATURE,
                                    "mint_wl": collection_mint.MINT_WL_SIGNATURE,
                                    "reveal": collection_reveal.REVEAL_SIGNATURE}


def test_a_sale_is_unique_on_its_chain():
    other = "0xC011000000000000000000000000000000000002"
    both = [profile(ADDR), profile(other, sale_pin=sale(other, address=SALE))]
    with pytest.raises(CR.CollectionRegistryError, match="two profiles"):
        CR.parse({"profiles": both})
    # a sale equal to ANOTHER profile's collection
    both = [profile(ADDR), profile(other, sale_pin=sale(other, address=ADDR))]
    with pytest.raises(CR.CollectionRegistryError, match="also a pinned collection"):
        CR.parse({"profiles": both})
    # the same CREATE address on two chains is two contracts
    assert len(CR.parse({"profiles": [profile(ADDR), profile(other, chain_id=46630,
                                                             sale_pin=sale(other, address=SALE))]})) == 2


def test_lookup_by_sale_address(monkeypatch):
    pin(monkeypatch, profile(ADDR), profile("0xC011000000000000000000000000000000000002",
                                            capabilities=("reveal",)))
    assert CR.profile_for_sale(4663, SALE.upper().replace("0X", "0x")).address == ADDR.lower()
    assert CR.profile_for_sale(4663, ADDR) is None
    assert CR.profile_for_sale(46630, SALE) is None


def test_the_sale_has_its_own_code_pin():
    code = {ADDR.lower(): "0x6000", SALE.lower(): "0x6001"}
    rpc = lambda m, a: code[a[0].lower()]  # noqa: E731
    import hashlib
    sha = lambda c: hashlib.sha256(bytes.fromhex(c[2:])).hexdigest()  # noqa: E731
    (p,) = CR.parse({"profiles": [profile(ADDR, runtime_sha256=sha("0x6000"),
                                          sale_pin=sale(ADDR, runtime_sha256=sha("0x6001")))]})
    assert CR.runtime_refusal(rpc, p) is None and CR.sale_runtime_refusal(rpc, p) is None
    code[SALE.lower()] = "0x6002"
    assert "pinned sale" in CR.sale_runtime_refusal(rpc, p) and CR.runtime_refusal(rpc, p) is None
    (bare,) = CR.parse({"profiles": [profile(ADDR, capabilities=("reveal",))]})
    assert "pins no sale" in CR.sale_runtime_refusal(rpc, bare)


def test_an_owner_pin_may_not_carry_a_sale(tmp_path):
    raw = profile(ADDR, capabilities=(), sale_pin=None)
    raw["sale"] = sale(ADDR)
    path = tmp_path / "collection_pins.json"
    path.write_text(json.dumps({"profiles": [raw]}))
    with pytest.raises(CR.CollectionRegistryError):
        CR.load_owner_pins(str(path))


def test_the_desk_pin_writer_with_sale_profile_is_accepted():
    """The exact shape ``polyrob-desk/contracts/script/pin.py::profile_of(..., sale=…)`` writes
    (with and without ``transfer_safe``)."""
    base = {"spec": "agent-nft-profile/1", "capabilities": ["mint", "reveal"], "chain_id": 4663,
            "address": ADDR.lower(), "runtime_sha256": "11" * 32, "deploy_block": 123,
            "max_supply": 6551,
            "accounts": [{"registry": erc6551.REGISTRY, "implementation": erc6551.ACCOUNT_V3_IMPL,
                          "salt": 0}],
            "journal_prefix": "POLYROB",
            "call_shapes": {"mint": "mint(address,uint256,uint256)", "reveal": "reveal(uint256[])"},
            "journal_log": "0x" + "ee" * 20,
            "sale": {"address": SALE.lower(), "runtime_sha256": "22" * 32,
                     "max_price_wei": 99000000000000000}}
    (p,) = CR.parse({"profiles": [base]})
    assert p.sale == CR.Sale(SALE.lower(), "22" * 32, 99 * 10 ** 15)
    ts = dict(base, accounts=[{"registry": erc6551.REGISTRY, "implementation": POLYROB_IMPL,
                               "salt": 0}], transfer_safe=transfer_safe())
    (p,) = CR.parse({"profiles": [ts]})
    assert p.sale.address == SALE.lower() and p.accounts[0].kind is AK.ERC6551P


_DESK_PIN = os.path.join(os.path.dirname(__file__), *[".."] * 4, "..", "polyrob-desk", "contracts",
                         "script", "pin.py")


@pytest.mark.skipif(not os.path.exists(_DESK_PIN), reason="../polyrob-desk is not beside this repo")
@pytest.mark.parametrize("with_sale", [False, True])
@pytest.mark.parametrize("with_transfer_safe", [False, True])
def test_the_desk_pin_writer_round_trips(with_sale, with_transfer_safe):
    """Import ``pin.py``'s own ``profile_of`` (placeholder inputs) and parse what it writes."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_desk_pin", _DESK_PIN)
    pin_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin_mod)
    sha = "0x" + "11" * 32
    sale_pin = ({"address": SALE, "runtime_sha256": "0x" + "22" * 32,
                 "max_price_wei": pin_mod.START_PRICE_WEI} if with_sale else None)
    ts = ({"account_impl_runtime_sha256": sha, "market": MARKET, "market_runtime_sha256": sha,
           "zone": "0x" + "c1" * 20, "zone_runtime_sha256": sha} if with_transfer_safe else None)
    impl = POLYROB_IMPL if with_transfer_safe else erc6551.ACCOUNT_V3_IMPL
    raw = pin_mod.profile_of(4663, ADDR.lower(), sha, 123, impl, "0x" + "ee" * 20, sale_pin, ts)
    (p,) = CR.parse({"profiles": [raw]})
    assert p.capabilities == (("mint", "reveal") if with_sale else ("reveal",))
    assert (p.sale is not None) == with_sale
    if with_sale:
        assert p.sale == CR.Sale(SALE.lower(), "22" * 32, 99 * 10 ** 15)
    # 111 W1: its spec-2 twin parses to the EQUAL profile
    assert CR.parse(CR.upgrade_document({"profiles": [raw]})) == (p,)
