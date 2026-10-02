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
from tests.collection_pins import pin, profile

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
