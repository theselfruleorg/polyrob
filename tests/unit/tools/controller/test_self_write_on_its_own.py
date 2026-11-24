"""128 D3: the agent's own scheduled run writes its self notes at once only when the owner
turned ``self.autonomous_writes`` on and the write quotes a real soul line."""
import types

import pytest

from tools.controller import doc_authoring as da

SOUL = "You decide your own acts.\nRaise awareness of The Selfrule Organization and POLYROB."


@pytest.fixture
def home(tmp_path):
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "identity.md").write_text(SOUL)
    return tmp_path


@pytest.fixture
def own_run(monkeypatch):
    import core.security.owner_turn as ot
    state = {"auto": True, "pref": True}
    monkeypatch.setattr(ot, "is_autonomous_goal_turn", lambda ctx, c=None: state["auto"])
    import core.prefs as prefs
    monkeypatch.setattr(prefs, "resolve", lambda key, uid, home, **k:
                        state["pref"] if key == "self.autonomous_writes" else None)
    return state


def _p(line):
    return types.SimpleNamespace(soul_line=line)


def test_a_cited_soul_line_applies_at_once(home, own_run):
    assert da._self_write_on_its_own(None, None, "u", home, _p(
        "raise awareness of the Selfrule   Organization")) == (True, "")


@pytest.mark.parametrize("line", [None, "", "short", "a line the soul does not hold at all"])
def test_no_or_a_false_citation_stays_a_draft_and_says_why(home, own_run, line):
    ok, note = da._self_write_on_its_own(None, None, "u", home, _p(line))
    assert ok is False and "soul_line" in note


def test_the_pref_off_or_a_non_scheduled_turn_is_a_plain_draft(home, own_run):
    own_run["pref"] = False
    assert da._self_write_on_its_own(None, None, "u", home, _p("You decide your own acts")) == (False, "")
    own_run["pref"], own_run["auto"] = True, False
    assert da._self_write_on_its_own(None, None, "u", home, _p("You decide your own acts")) == (False, "")


def test_an_unreadable_soul_cites_nothing(tmp_path):
    assert da._soul_cited(tmp_path / "missing", "You decide your own acts") is False
