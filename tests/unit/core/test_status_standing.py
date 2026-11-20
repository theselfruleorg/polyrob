"""The owner's standing money rules on /status: the income rule and the mandates.

Each section reads through its module's own reader, says "none" for no store, and
is ``unavailable`` (never "none") for a store it could not read.
"""
import json
import os
import time

import pytest

import core.status_sections as S
from core.status_render import BRIEF_SECTIONS, _SECTION_TITLES
from core.status_snapshot import (SECTION_ORDER, STATE_DEGRADED, STATE_OK, STATE_UNAVAILABLE,
                                  build_status_snapshot)
from core.status_standing import income_rule_section, mandates_section
from core.wallet import income_rule as R
from core.wallet import standing_grant as SG


def _ctx(tmp_path):
    return S.SectionContext(uid="u1", data_dir=str(tmp_path), now=time.time(), window_sec=86400)


def test_the_slots_are_in_the_full_view_not_the_brief():
    for name in ("income_rule", "mandates"):
        assert name in SECTION_ORDER and name in S.CONTRIBUTED_SLOTS
        assert name in _SECTION_TITLES and name not in BRIEF_SECTIONS
        assert S.provider(name) is not None


def test_no_store_is_none(tmp_path):
    sec = S.build_slot("income_rule", _ctx(tmp_path))
    assert sec.state == STATE_OK and sec.lines == ["Rule: none set — no income is allocated"]
    assert sec.data["owed_usd"] == 0
    sec = S.build_slot("mandates", _ctx(tmp_path))
    assert sec.state == STATE_OK and sec.lines[0].startswith("none live")


@pytest.mark.parametrize("name,store", [("income_rule", R.STORE_NAME),
                                        ("mandates", SG.STORE_NAME)])
def test_an_unreadable_store_is_unavailable_never_none(tmp_path, name, store):
    os.makedirs(tmp_path / "wallet")
    (tmp_path / "wallet" / store).write_text("{not json")
    sec = S.build_slot(name, _ctx(tmp_path))
    assert sec.state == STATE_UNAVAILABLE and sec.reason
    assert not any("none" in line for line in sec.lines)


def test_the_snapshot_names_an_unreadable_rule_store(tmp_path):
    os.makedirs(tmp_path / "wallet")
    (tmp_path / "wallet" / R.STORE_NAME).write_text("[]")
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["income_rule"].state == STATE_UNAVAILABLE
    assert any(u.startswith("income_rule") for u in snap.unavailable_sources)


def test_the_debt_and_an_unverified_rule_are_said(tmp_path, monkeypatch):
    state = R.empty_state()
    state["rule"] = {"id": "r_1", "created_at": 1.0, "kinds": ["fees"], "percent": 10.0,
                     "chain": "base", "token": "0xT", "sig": "0xbad"}
    state["entries"] = [{"key": "k", "state": R.OWED, "owed_usd": 3.5}]
    os.makedirs(tmp_path / "wallet")
    (tmp_path / "wallet" / R.STORE_NAME).write_text(json.dumps(state))
    monkeypatch.setattr(R, "_verified", lambda rule: False)
    sec = income_rule_section(str(tmp_path))
    assert "does not verify" in sec.lines[0]
    assert "Owed to the rule: $3.50" in sec.lines
    assert sec.data["owed_usd"] == 3.5 and sec.data["verified"] is False
    assert [h.key for h in sec.health] == ["income_rule_unverified"]


def _mandate(**kw):
    now = time.time()
    row = dict(id="m_1", created_at=now, ends_at=now + 86400, verbs=["agent_nft_buy"],
               chains=["base"], assets=[], band_usd=(1.0, 5.0), count=3, total_usd=10.0)
    row.update(kw)
    return SG.Mandate(**row)


def test_a_live_mandate_is_listed_and_one_that_does_not_verify_is_named(tmp_path, monkeypatch):
    path = SG.store_path(str(tmp_path))
    os.makedirs(os.path.dirname(path))
    SG._write(path, [_mandate(), _mandate(id="m_2", ended_at=1.0)])
    monkeypatch.setattr(SG, "_verified", lambda m: True)
    sec = mandates_section(str(tmp_path))
    assert sec.state == STATE_OK and "m_1 [live]" in sec.lines[0]
    assert sec.lines[-1] == "1 ended or used up"
    monkeypatch.setattr(SG, "_verified", lambda m: False)
    sec = S.build_slot("mandates", _ctx(tmp_path))
    assert sec.state == STATE_DEGRADED and "grants nothing" in sec.lines[0]
    assert sec.health[0].key == "mandate_unverified"
