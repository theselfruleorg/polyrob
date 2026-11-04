"""099 W1/W4: the offer manifest, the offer store and the order state machine."""
import copy

import pytest

from core.commerce import offer as om
from core.commerce import orders as ords
from core.commerce import store as st

MANIFEST = {
    "id": "custom-art", "title": "Custom art NFT", "summary": "A 1/1 art piece minted to you.",
    "pricing": {"kind": "fixed", "amount_usd": "5"},
    "rails": [{"asset": "usdc-base", "mode": "transfer"}],
    "inputs": {"delivery_address": {"type": "string", "format": "evm_address",
                                    "role": "recipient"},
               "brief": {"type": "string", "maxLength": 280, "role": "untrusted_text"}},
    "deliverable": {"kind": "onchain_asset", "proof": "tx_receipt"},
    "fulfillment": {"playbook": "offer-custom-art", "tools": ["defi_trade", "code_execution"],
                    "budget_per_order": {"spend_usd": 0.5}, "deadline": "PT30M"},
    "listings": ["native:telegram", "a2a", "http402"],
}


def _m(**over):
    d = copy.deepcopy(MANIFEST)
    d.update(over)
    return d


def test_manifest_validates_and_normalizes():
    m = om.validate(MANIFEST)
    assert m.amount_usd == "5.00" and m.deadline_min == 30
    assert m.role_field("recipient") == "delivery_address"
    assert m.native_surfaces() == ("telegram",)


@pytest.mark.parametrize("over,needle", [
    ({"pricing": {"kind": "recurring"}}, "not supported yet"),
    ({"listings": ["native:email"]}, "email is never a buying channel"),
    ({"fulfillment": {"playbook": "skill:offer/x", "tools": ["defi_trade"]}}, "playbook"),
    ({"inputs": {}}, "role recipient"),
    ({"pricing": {"kind": "fixed", "amount_usd": "0"}}, "between"),
])
def test_manifest_refusals(over, needle):
    with pytest.raises(om.OfferError, match=needle):
        om.validate(_m(**over))


def test_inputs_are_validated_against_roles():
    m = om.validate(MANIFEST)
    out = om.validate_inputs(m, {"delivery_address": "0x" + "AB" * 20, "brief": "a cat"})
    assert out["delivery_address"] == "0x" + "ab" * 20
    with pytest.raises(om.OfferError, match="0x address"):
        om.validate_inputs(m, {"delivery_address": "vitalik.eth"})
    with pytest.raises(om.OfferError, match="unknown input"):
        om.validate_inputs(m, {"delivery_address": "0x" + "ab" * 20, "x": 1})


def test_offer_lifecycle_owner_only(tmp_path):
    p = str(tmp_path / "commerce.db")
    o = st.save_draft("u1", MANIFEST, path=p)
    assert o.state == st.DRAFT and o.version == 0
    with pytest.raises(st.CommerceError, match="only the owner"):
        st.go_live("custom-art", "u1", actor="agent", expect_hash="x", path=p)
    h = st.manifest_hash(o.manifest)
    o = st.start_setup("custom-art", "u1", budget_usd=3, actor=st.OWNER_SEAT, path=p)
    assert o.state == st.SETUP and o.setup_budget_usd == 3
    o = st.go_live("custom-art", "u1", actor=st.OWNER_SEAT, expect_hash=h, path=p)
    assert o.state == st.LIVE and o.version == 1
    assert [m.id for m in st.live_offers(listing="a2a", path=p)] == ["custom-art"]
    # an edit is staged; the live version stays until the owner confirms the new hash
    st.save_draft("u1", _m(title="Custom art NFT v2"), path=p)
    assert st.live_version("custom-art", path=p).title == "Custom art NFT"
    with pytest.raises(st.CommerceError, match="changed after this card"):
        st.go_live("custom-art", "u1", actor=st.OWNER_SEAT, expect_hash=h, path=p)
    o = st.go_live("custom-art", "u1", actor=st.OWNER_SEAT,
                   expect_hash=st.manifest_hash(st.get("custom-art", path=p).manifest), path=p)
    assert o.version == 2 and st.live_version("custom-art", 1, path=p).title == "Custom art NFT"
    assert st.set_state("custom-art", "u1", "paused", actor="agent", path=p).state == st.PAUSED
    with pytest.raises(st.CommerceError):
        st.set_state("custom-art", "u1", "live", actor="agent", path=p)
    assert st.set_state("custom-art", "u1", "live", actor=st.OWNER_SEAT, path=p).is_live


def test_resource_rules(tmp_path):
    p = str(tmp_path / "commerce.db")
    st.save_draft("u1", MANIFEST, path=p)
    with pytest.raises(st.CommerceError, match="runtime_hash"):
        st.add_resource("custom-art", "u1", "contract", {}, chain="base",
                        address="0x" + "11" * 20, path=p)
    with pytest.raises(st.CommerceError, match="secret REF"):
        st.add_resource("custom-art", "u1", "account", {"api_key": "k"}, path=p)
    with pytest.raises(st.CommerceError, match="callable functions"):
        st.add_resource("custom-art", "u1", "contract", {"runtime_hash": "0x" + "ab" * 32},
                        chain="base", address="0x" + "11" * 20, path=p)
    st.add_resource("custom-art", "u1", "contract", {"runtime_hash": "0x" + "ab" * 32,
                                                     "functions": ["mint(address,string)"]},
                    chain="Base", address="0x" + "11" * 20, path=p)
    assert st.contract_resource("custom-art", "base", "0x" + "11" * 20, path=p) is not None


def test_unreadable_store_is_not_empty(tmp_path):
    p = tmp_path / "commerce.db"
    p.write_bytes(b"not a database at all" * 50)
    with pytest.raises(st.CommerceUnreadable):
        st.get("x", path=str(p))


def test_order_machine_happy_path_and_envelope(tmp_path):
    p = str(tmp_path / "commerce.db")
    o = ords.create(offer_id="custom-art", offer_version=1, user_id="u1", channel="telegram",
                    buyer="42", inputs={"a": 1}, amount_usd="5.00", spend_budget=0.5, path=p)
    assert o.status == ords.AWAITING and o.id.startswith("ord_")
    ords.attach_invoice(o.id, "inv_1", "5.00", path=p)
    assert ords.mark_paid("inv_1", path=p).status == ords.PAID
    assert ords.mark_paid("inv_1", path=p) is None            # idempotent
    tok = ords.claim_fulfillment(o.id, path=p)
    assert tok and ords.claim_fulfillment(o.id, path=p) is None
    assert ords.set_goal(o.id, tok, "g1", path=p)
    assert ords.reserve_spend(o.id, 0.3, path=p)
    assert not ords.reserve_spend(o.id, 0.3, path=p)          # over the envelope
    d = ords.deliver(o.id, {"tx": "0xabc"}, path=p)
    assert d.status == ords.DELIVERED and len(d.receipt["sha256"]) == 64
    assert not ords.reserve_spend(o.id, 0.01, path=p)         # consumed
    assert ords.dispute(o.id, "wrong art", path=p)
    assert ords.accept(o.id, path=p)


def test_stale_claim_is_reclaimed(tmp_path):
    p = str(tmp_path / "commerce.db")
    o = ords.create(offer_id="x", offer_version=1, user_id="u1", channel="a2a", buyer="b",
                    inputs={}, amount_usd="1.00", spend_budget=0, path=p, now=1000)
    ords.attach_invoice(o.id, "inv_2", "1.00", path=p)
    ords.mark_paid("inv_2", path=p)
    ords.claim_fulfillment(o.id, path=p, now=1000)
    stale = ords.stale_claims(now=1000 + ords.CLAIM_STALE_S + 1, path=p)
    assert [s.id for s in stale] == [o.id]
    assert ords.reclaim(stale[0], path=p)


def test_the_live_card_hash_pins_the_contract_resources_too(tmp_path):
    """Review C8: a contract resource added after the go-live card was sent changes
    the card hash, so the Confirm refuses instead of granting it authority."""
    p = str(tmp_path / "commerce.db")
    o = st.save_draft("owner", MANIFEST, path=p)
    shown = st.card_hash(st.get(o.id, path=p), path=p)
    assert shown == st.manifest_hash(o.manifest)       # no contracts: the manifest alone
    st.add_resource(o.id, "owner", "contract", {"runtime_hash": "0x" + "ab" * 32,
                                                "functions": ["withdraw(address)"]},
                    chain="base", address="0x" + "33" * 20, path=p)
    assert st.card_hash(st.get(o.id, path=p), path=p) != shown
    with pytest.raises(st.CommerceError, match="changed after this card"):
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=shown, path=p)
    fresh = st.card_hash(st.get(o.id, path=p), path=p)
    assert st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=fresh,
                      path=p).state == st.LIVE
