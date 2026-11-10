"""069 v4 A2 — the owner-pinned collection registry.

Empty by default; a file the service can write, an unreadable file or an off-schema one is
"cannot be trusted" (never "nothing pinned"); an unknown spec, capability, account version or
call shape refuses the whole file.
"""
import copy
import json
import os

import pytest

from core.wallet import account_journal, collection_registry as CR, erc6551
from tests.collection_pins import (MARKET, POLYROB_IMPL, RUNTIME_SHA256, pin, polyrob_profile,
                                   profile, transfer_safe)

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
    (lambda d: d["profiles"][0].update(spec="agent-nft-profile/2"), "spec"),
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


# ------------------------------------------------------------------ PolyrobAccount (transfer_safe)
def test_without_transfer_safe_only_account_v3_is_accepted():
    (p,) = CR.parse(_doc())
    assert p.transfer_safe is None
    assert p.accounts[0].kind == CR.KIND_ACCOUNT_V3 and not p.accounts[0].is_polyrob
    assert p.accounts[0].runtime_sha256 is None


def test_with_transfer_safe_accounts0_is_the_pinned_polyrob_account():
    (p,) = CR.parse({"profiles": [polyrob_profile(ADDR, journal_log="0x" + "ee" * 20)]})
    v = p.accounts[0]
    assert (v.implementation, v.kind, v.runtime_sha256) == (POLYROB_IMPL, CR.KIND_POLYROB, RUNTIME_SHA256)
    assert v.is_polyrob and p.account_version(POLYROB_IMPL.upper().replace("0X", "0x")) == v
    assert p.transfer_safe.market == MARKET and p.transfer_safe.zone_runtime_sha256 == RUNTIME_SHA256
    assert p.account_version(erc6551.ACCOUNT_V3_IMPL) is None


def test_a_later_account_version_beside_a_polyrob_account_must_be_account_v3():
    raw = polyrob_profile(ADDR)
    raw["accounts"].append({"registry": erc6551.REGISTRY, "implementation": erc6551.ACCOUNT_V3_IMPL,
                            "salt": 0})
    (p,) = CR.parse({"profiles": [raw]})
    assert [v.kind for v in p.accounts] == [CR.KIND_POLYROB, CR.KIND_ACCOUNT_V3]
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
    assert p.accounts[0].is_polyrob and p.journal_log == "0x" + "ee" * 20
