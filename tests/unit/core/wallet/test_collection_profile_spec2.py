"""111 W1 / §2.3 — the profile schema ``agent-nft-profile/2`` and the spec-1 mapping.

Spec 2 names each account's KIND (a row of ``core.wallet.account_kinds``) and moves the
collection's sale rails to ``standard.erc6551p``. Spec 1 is still read and mapped in memory: a
spec-1 file and its spec-2 twin parse to EQUAL profiles (the fail-closed schema change must not
drop or change a pin).
"""
import copy
import json

import pytest

from core.wallet import account_kinds as AK, collection_registry as CR, erc6551
from tests.collection_pins import (MARKET, POLYROB_IMPL, RUNTIME_SHA256, ZONE, polyrob_profile,
                                   profile)

ADDR = "0xC011000000000000000000000000000000000001"
V3 = {"kind": "tokenbound_v3", "registry": erc6551.REGISTRY,
      "implementation": erc6551.ACCOUNT_V3_IMPL, "salt": 0}
P = {"kind": "erc6551p", "registry": erc6551.REGISTRY, "implementation": POLYROB_IMPL,
     "salt": 0, "runtime_sha256": RUNTIME_SHA256}
STD = {"erc6551p": {"market": MARKET, "market_runtime_sha256": RUNTIME_SHA256, "zone": ZONE,
                    "zone_runtime_sha256": RUNTIME_SHA256}}


def _v2(**over):
    raw = profile(ADDR)
    raw["spec"] = CR.SPEC_V2
    raw["accounts"] = [dict(P)]
    raw["standard"] = copy.deepcopy(STD)
    raw.update(over)
    return raw


def _one(raw):
    (p,) = CR.parse({"profiles": [raw]})
    return p


def test_a_spec2_profile_parses_with_its_kinds_and_standard():
    p = _one(_v2(accounts=[dict(P), dict(V3)], journal_log="0x" + "ee" * 20))
    assert p.spec == CR.SPEC_V2 and p.source_spec == CR.SPEC_V2
    assert [v.kind for v in p.accounts] == [AK.ERC6551P, AK.TOKENBOUND_V3]
    assert p.accounts[0].runtime_sha256 == RUNTIME_SHA256 and p.accounts[1].runtime_sha256 is None
    assert p.erc6551p == CR.Erc6551pStandard(MARKET, RUNTIME_SHA256, ZONE, RUNTIME_SHA256)
    assert p.sale is not None and p.journal_log == "0x" + "ee" * 20


def test_an_erc6551p_collection_with_no_market_pins_without_the_standard_block():
    raw = _v2()
    raw.pop("standard")
    assert _one(raw).erc6551p is None


def test_a_null_sale_in_spec2_is_no_sale():
    raw = _v2(capabilities=["reveal"], sale=None)
    assert _one(raw).sale is None


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r["accounts"][0].update(kind="polyrob_account"), "not an account kind"),
    (lambda r: r["accounts"][0].pop("kind"), "must have exactly"),
    (lambda r: r["accounts"][0].pop("runtime_sha256"), "needs its runtime_sha256"),
    (lambda r: r["accounts"][0].update(implementation=erc6551.ACCOUNT_V3_IMPL), "is AccountV3"),
    (lambda r: r["accounts"][0].update(extra=1), "must have exactly"),
    (lambda r: r.update(accounts=[dict(V3, runtime_sha256=RUNTIME_SHA256)]), "pinned by this build"),
    (lambda r: r.update(accounts=[dict(V3, implementation=POLYROB_IMPL)]), "one implementation"),
    (lambda r: r.update(accounts=[dict(V3)]), "no account version is an ERC-6551P account"),
    (lambda r: r["standard"].update(seaport={}), "unknown standard"),
    (lambda r: r["standard"]["erc6551p"].pop("zone"), "exactly"),
    (lambda r: r["standard"]["erc6551p"].update(market="0x12"), "market"),
    (lambda r: r.update(standard={}), "naming a collection standard"),
    (lambda r: r.update(transfer_safe={}), "unknown field"),
    (lambda r: r.update(sale={"address": "0x" + "ab" * 20}), "exactly"),
])
def test_an_off_schema_spec2_profile_refuses_the_registry(mutate, needle):
    raw = _v2()
    mutate(raw)
    with pytest.raises(CR.CollectionRegistryError, match=needle):
        CR.parse({"profiles": [raw]})


def test_spec1_may_not_carry_spec2_keys():
    raw = profile(ADDR)
    raw["standard"] = copy.deepcopy(STD)
    with pytest.raises(CR.CollectionRegistryError, match="unknown field"):
        CR.parse({"profiles": [raw]})
    raw = profile(ADDR)
    raw["accounts"][0]["kind"] = "tokenbound_v3"
    with pytest.raises(CR.CollectionRegistryError, match="must have exactly"):
        CR.parse({"profiles": [raw]})


# ------------------------------------------------------------------ spec 1 ≡ spec 2
def _spec1_cases():
    v3_only = profile(ADDR)
    v3_reveal = profile(ADDR, capabilities=("reveal",), journal_prefix="ACME")
    p6551 = polyrob_profile(ADDR, journal_log="0x" + "ee" * 20)
    p6551_two = polyrob_profile(ADDR)
    p6551_two["accounts"].append({"registry": erc6551.REGISTRY,
                                  "implementation": erc6551.ACCOUNT_V3_IMPL, "salt": 0})
    owner_pin = profile(ADDR, capabilities=(), sale_pin=None)
    return {"v3": v3_only, "v3-reveal": v3_reveal, "erc6551p": p6551, "two-kinds": p6551_two,
            "owner-pin": owner_pin}


@pytest.mark.parametrize("case", sorted(_spec1_cases()))
def test_a_spec1_file_and_its_spec2_twin_parse_to_equal_profiles(case):
    spec1 = {"profiles": [_spec1_cases()[case]]}
    spec2 = CR.upgrade_document(copy.deepcopy(spec1))
    assert spec2["profiles"][0]["spec"] == CR.SPEC_V2
    assert "transfer_safe" not in spec2["profiles"][0]
    assert all("kind" in a for a in spec2["profiles"][0]["accounts"])
    a, b = CR.parse(spec1), CR.parse(spec2)
    assert a == b
    assert (a[0].source_spec, b[0].source_spec) == (CR.SPEC_V1, CR.SPEC_V2)
    # the written spec-2 form is plain JSON and round-trips
    assert CR.parse(json.loads(json.dumps(spec2))) == a
    # upgrading twice changes nothing
    assert CR.upgrade_document(spec2) == spec2


def test_the_spec1_transfer_safe_maps_to_the_erc6551p_account_and_standard():
    spec2 = CR.upgrade_document({"profiles": [polyrob_profile(ADDR)]})
    (row,) = spec2["profiles"]
    assert row["accounts"][0]["kind"] == "erc6551p"
    assert row["accounts"][0]["runtime_sha256"] == RUNTIME_SHA256
    assert row["standard"] == STD


def test_an_unknown_spec_does_not_upgrade():
    raw = profile(ADDR)
    raw["spec"] = "agent-nft-profile/9"
    with pytest.raises(CR.CollectionRegistryError):
        CR.upgrade_document({"profiles": [raw]})


def test_account_of_and_version_for_account_are_one_rule():
    p = _one(_v2(accounts=[dict(P), dict(V3)]))
    account, version = p.account_of(7)
    assert version is p.accounts[0]
    assert p.version_for_account(7, account) is p.accounts[0]
    v3_account = erc6551.account_address(p.chain_id, p.address, 7,
                                         implementation=erc6551.ACCOUNT_V3_IMPL)
    assert p.version_for_account(7, v3_account) is p.accounts[1]
