"""067 P5a: status sections a provider fills — core names the slots."""
import pytest

import core.status_sections as S
from core.status_snapshot import (SECTION_ORDER, STATE_OK, STATE_UNAVAILABLE, Section,
                                  build_status_snapshot)


def test_every_slot_is_a_section_order_name():
    assert set(S.CONTRIBUTED_SLOTS) <= set(SECTION_ORDER)
    assert set(S.CONTRIBUTED_SLOTS) == {
        "wallet", "custody", "money", "economics", "liquidity", "collectibles",
        "creations", "room_actions"}


def test_the_in_core_providers_fill_every_slot():
    covered = {slot for slots in S._IN_CORE_PROVIDERS.values() for slot in slots}
    assert covered == set(S.CONTRIBUTED_SLOTS)
    for slot in S.CONTRIBUTED_SLOTS:
        assert S.provider(slot) is not None, slot


def test_a_provider_cannot_add_a_section():
    with pytest.raises(ValueError, match="not a slot"):
        S.register_status_section("my_pack_section", lambda ctx: None)
    with pytest.raises(TypeError):
        S.register_status_section("wallet", "nope")


@pytest.fixture
def empty(monkeypatch):
    S.provider("wallet")                        # load the in-core providers first
    monkeypatch.setattr(S, "_PROVIDERS", {})
    monkeypatch.setattr(S, "_IMPORT_ERRORS", {})


def test_an_empty_slot_is_one_not_installed_line(empty, tmp_path):
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert tuple(snap.sections) == SECTION_ORDER
    for slot in S.CONTRIBUTED_SLOTS:
        sec = snap.sections[slot]
        assert (sec.state, sec.lines) == (STATE_OK, [S.NOT_INSTALLED]), slot
    # stated, not omitted — and not counted as an unreadable source
    assert not any(u.split(" ")[0] in S.CONTRIBUTED_SLOTS for u in snap.unavailable_sources)


def test_a_provider_that_did_not_import_is_unavailable_with_the_reason(empty, tmp_path):
    S._IMPORT_ERRORS["wallet"] = "ImportError: no module named web3"
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["wallet"].state == STATE_UNAVAILABLE
    assert "wallet (ImportError: no module named web3)" in snap.unavailable_sources


def test_a_registered_provider_fills_its_slot(empty, tmp_path):
    seen = []

    def build(ctx):
        seen.append((ctx.uid, ctx.data_dir))
        return Section(name="wallet", lines=["from a pack"])
    S.register_status_section("wallet", build, tenant=False, source="pack:wallet")
    snap = build_status_snapshot("", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["wallet"].lines == ["from a pack"]
    assert seen == [("", str(tmp_path))]


def test_a_tenant_slot_without_a_tenant_is_not_built(empty, tmp_path):
    S.register_status_section("creations", lambda ctx: 1 / 0)
    snap = build_status_snapshot("", data_dir=str(tmp_path), include_money=False)
    sec = snap.sections["creations"]
    assert (sec.state, sec.reason) == (STATE_UNAVAILABLE, "no tenant (empty user_id)")


def test_a_raising_or_misnamed_provider_is_unavailable(empty, tmp_path):
    S.register_status_section("creations", lambda ctx: 1 / 0)
    S.register_status_section("wallet", lambda ctx: Section(name="money"), tenant=False)
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["creations"].state == STATE_UNAVAILABLE
    assert "ZeroDivisionError" in snap.sections["creations"].reason
    assert snap.sections["wallet"].state == STATE_UNAVAILABLE


def test_the_money_slot_hands_its_ledger_to_economics(monkeypatch, tmp_path):
    seen = {}
    real = S.provider("economics")

    def spy(ctx):
        seen["ledger"] = ctx.ledger
        return real.build(ctx)
    monkeypatch.setitem(S._PROVIDERS, "economics", S.SectionProvider(
        "economics", spy, True, "test"))
    ledger = {"runtime": {"provider_balance_usd": 3.0}, "treasury": {}}
    build_status_snapshot("u1", data_dir=str(tmp_path), ledger=ledger)
    assert seen["ledger"] is ledger


def test_moved_builders_stay_importable_from_the_snapshot():
    import core.status_money as M
    import core.status_room_actions as R
    import core.status_snapshot as ss
    for name in ("_wallet_section", "_positions_line", "_money_section", "CREATION_VERBS",
                 "_creations_section", "moves_section", "NFT_MOVE_VERBS",
                 "_collectibles_section"):
        assert getattr(ss, name) is getattr(M, name), name
    assert ss._room_actions_section is R._room_actions_section
    with pytest.raises(AttributeError):
        ss.no_such_name  # noqa: B018


def test_a_patched_snapshot_builder_reaches_the_slot(monkeypatch, tmp_path):
    """The webview tests (and callers) patch ``core.status_snapshot.<builder>``."""
    monkeypatch.setattr("core.status_snapshot._creations_section",
                        lambda uid, data_dir: Section(name="creations", lines=["patched"]))
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["creations"].lines == ["patched"]
