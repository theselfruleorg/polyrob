"""125 S20: a list preference changes by ONE entry under the file lock (B-H5).

Before: every caller read the list outside the lock, appended, and wrote the whole
list back — two answers of "[n]ever" at once lost one deny entry, and the refused
action could run again."""
import threading

import pytest

from core.prefs import (list_pending_pref_changes, load_preferences, preferences_path,
                        promote_pref_change, propose_pref_change)
from core.prefs_lists import add_list_entries
from tools.controller.approval_interactive import InteractiveCLIApprover


def _deny(home):
    return load_preferences(home, "u1").get("approvals.deny", [])


def test_a_stale_read_does_not_lose_an_entry(tmp_path, monkeypatch):
    """The second answer read the list before the first one wrote it."""
    prov = InteractiveCLIApprover(user_id="u1", home_dir=tmp_path)
    prov._handle_never("first_action")
    import core.prefs as prefs
    real = prefs.load_preferences
    calls = {"n": 0}

    def stale(*a, **k):
        calls["n"] += 1
        return {} if calls["n"] == 1 else real(*a, **k)
    monkeypatch.setattr(prefs, "load_preferences", stale)
    prov._handle_never("second_action")
    monkeypatch.setattr(prefs, "load_preferences", real)
    assert sorted(_deny(tmp_path)) == ["first_action", "second_action"]


def test_twenty_concurrent_nevers_keep_every_entry(tmp_path):
    names = [f"action_{i}" for i in range(20)]
    barrier = threading.Barrier(len(names))

    def answer(name):
        prov = InteractiveCLIApprover(user_id="u1", home_dir=tmp_path)
        barrier.wait()
        prov._handle_never(name)
    threads = [threading.Thread(target=answer, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(_deny(tmp_path)) == sorted(names)


def test_add_reports_only_what_it_added(tmp_path):
    ok, err, added = add_list_entries(tmp_path, "u1", "approvals.require", ["a", "b"])
    assert ok and not err and added == ["a", "b"]
    ok, err, added = add_list_entries(tmp_path, "u1", "approvals.require", ["b", "c"])
    assert ok and added == ["c"]
    assert load_preferences(tmp_path, "u1")["approvals.require"] == ["a", "b", "c"]


def test_an_invalid_stored_list_refuses_and_is_kept(tmp_path, monkeypatch):
    p = preferences_path(tmp_path, "u1")
    p.parent.mkdir(parents=True, exist_ok=True)
    before = '[approvals]\ndeny = ["a"]\n'
    p.write_text(before, encoding="utf-8")
    import core.prefs as prefs
    real = prefs.validate_pref
    monkeypatch.setattr(prefs, "validate_pref", lambda k, v: (
        (False, None, "approvals.deny: too many") if v == ["a"] else real(k, v)))
    ok, err, added = add_list_entries(tmp_path, "u1", "approvals.deny", ["x"])
    assert not ok and "approvals.deny" in err and added == []
    assert p.read_text(encoding="utf-8") == before


def test_a_stored_text_list_keeps_its_read_meaning(tmp_path):
    p = preferences_path(tmp_path, "u1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[approvals]\ndeny = "a, b"\n', encoding="utf-8")
    assert add_list_entries(tmp_path, "u1", "approvals.deny", ["c"])[0]
    assert _deny(tmp_path) == ["a", "b", "c"]


def test_a_non_list_key_refuses(tmp_path):
    ok, err, _ = add_list_entries(tmp_path, "u1", "style.verbosity", ["terse"])
    assert not ok and "list" in err


@pytest.mark.parametrize("bad", [[""], ["   "], []])
def test_an_empty_entry_refuses(tmp_path, bad):
    ok, _err, _ = add_list_entries(tmp_path, "u1", "approvals.deny", bad)
    assert not ok


def test_an_add_entry_proposal_applies_against_the_current_list(tmp_path):
    """A queued widening stores the ENTRY, not the list it saw: an entry the owner
    added meanwhile survives the promote."""
    ok, pid = propose_pref_change("u1", "shell.allow", None, tmp_path,
                                  op="add_entry", entry="git status*")
    assert ok, pid
    (item,) = list_pending_pref_changes("u1", tmp_path)
    assert "git status*" in item["preview"] and "add" in item["preview"]
    assert add_list_entries(tmp_path, "u1", "shell.allow", ["ls*"])[0]
    ok, msg = promote_pref_change(pid, user_id="u1", home_dir=tmp_path)
    assert ok, msg
    assert load_preferences(tmp_path, "u1")["shell.allow"] == ["ls*", "git status*"]
    assert list_pending_pref_changes("u1", tmp_path) == []
