"""046 step 5: `trustMode: onchain` must be EARNED, not asserted.

Until now `EIP8004_ONCHAIN_ENABLED=true` plus two env vars flipped the publicly
served file from `local` to `onchain` and emitted a `registrations[]` entry —
with no code in this repository ever having signed a registration. The existing
honesty fix marked that entry `attestation="operator"` so a reader could tell it
was a claim. That was the right answer while there was no write path.

There is one now, so the claim can be backed: `register_agent` records what it
actually did — chain, registry, agentId, tx hash — into the instance identity
home, and ONLY a record written by a confirmed on-chain receipt yields
`attestation="verified"`.

⚠️ The operator claim still works and still says `operator`. Removing it would
break an instance that genuinely registered by hand. The two are never conflated.
"""
import json

import pytest

from modules.eip8004.registration import build_registration_file

REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    for k in ("EIP8004_ONCHAIN_ENABLED", "EIP8004_AGENT_ID",
              "EIP8004_IDENTITY_REGISTRY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    return tmp_path


def _record(home, **over):
    from core.instance import erc8004_record_path
    p = erc8004_record_path(home, "rob")
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"chain": "base", "chain_id": 8453, "registry": REGISTRY,
           "agent_id": 42, "tx_hash": "0x" + "ab" * 32,
           "registered_at": "2026-09-15T00:00:00+00:00"}
    doc.update(over)
    p.write_text(json.dumps(doc))
    return p


def test_no_record_and_no_claim_stays_local():
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "local"
    assert reg.registrations == []


def test_a_verified_record_earns_onchain_trust_mode(_home):
    _record(_home)
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "onchain"
    assert len(reg.registrations) == 1
    assert reg.registrations[0].agentId == 42


def test_a_verified_record_is_attested_as_VERIFIED_not_operator(_home):
    """The distinction the whole fix is about: this one is backed by a
    transaction this code signed and confirmed."""
    _record(_home)
    reg = build_registration_file("https://example.test")
    assert reg.registrations[0].attestation == "verified"


def test_the_registry_identifier_is_caip_shaped(_home):
    _record(_home)
    reg = build_registration_file("https://example.test")
    assert reg.registrations[0].agentRegistry == f"eip155:8453:{REGISTRY}"


def test_an_operator_claim_still_works_and_still_says_operator(monkeypatch):
    """An instance that registered by hand must not be broken by this."""
    monkeypatch.setenv("EIP8004_ONCHAIN_ENABLED", "true")
    monkeypatch.setenv("EIP8004_AGENT_ID", "7")
    monkeypatch.setenv("EIP8004_IDENTITY_REGISTRY", REGISTRY)
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "onchain"
    assert reg.registrations[0].attestation == "operator"


def test_a_verified_record_wins_over_an_operator_claim(_home, monkeypatch):
    """A fact beats an assertion, and they must never both be emitted — two
    registrations[] entries would read as two identities."""
    _record(_home, agent_id=42)
    monkeypatch.setenv("EIP8004_ONCHAIN_ENABLED", "true")
    monkeypatch.setenv("EIP8004_AGENT_ID", "7")
    monkeypatch.setenv("EIP8004_IDENTITY_REGISTRY", REGISTRY)
    reg = build_registration_file("https://example.test")
    assert len(reg.registrations) == 1
    assert reg.registrations[0].agentId == 42
    assert reg.registrations[0].attestation == "verified"


def test_a_corrupt_record_does_not_claim_anything(_home):
    """⚠️ Fail to `local`. An unreadable record is not evidence of a
    registration, and claiming one on a broken file is the worst outcome."""
    from core.instance import erc8004_record_path
    p = erc8004_record_path(_home, "rob")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "local"
    assert reg.registrations == []


def test_a_record_missing_its_agent_id_claims_nothing(_home):
    _record(_home, agent_id=None)
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "local"


def test_a_record_missing_its_tx_hash_claims_nothing(_home):
    """No transaction hash means nothing verified it — that is an operator
    claim wearing a verified record's filename."""
    _record(_home, tx_hash=None)
    reg = build_registration_file("https://example.test")
    assert reg.trustMode == "local"


# --- the writer -----------------------------------------------------------

def test_the_record_is_written_only_with_a_confirmed_receipt(_home):
    from core.instance import load_erc8004_record, save_erc8004_record
    save_erc8004_record(_home, "rob", chain="base", chain_id=8453,
                        registry=REGISTRY, agent_id=42, tx_hash="0x" + "cd" * 32)
    rec = load_erc8004_record(_home, "rob")
    assert rec["agent_id"] == 42 and rec["registered_at"]


def test_saving_without_a_tx_hash_is_refused(_home):
    """⚠️ The record's whole purpose is to be evidence. A row with no
    transaction is an assertion, and we already have a field for those."""
    from core.instance import save_erc8004_record
    with pytest.raises(ValueError):
        save_erc8004_record(_home, "rob", chain="base", chain_id=8453,
                            registry=REGISTRY, agent_id=42, tx_hash=None)


def test_an_absent_record_reads_as_none_not_a_crash(_home):
    from core.instance import load_erc8004_record
    assert load_erc8004_record(_home, "rob") is None
