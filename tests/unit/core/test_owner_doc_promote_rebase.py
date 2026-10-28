"""Promote applies a pending owner.md draft's CHANGE to the CURRENT active doc.

Prod 2026-10-15: a draft of owner.md would have moved ~60 live rules (stops
included) under '## Superseded' on approval — promote activated the whole
pending body. Prod 2026-10-10: a stale 'REBUYS RESUMED … 0a1b2c3d4e5f' line,
already retired, came back live through a promoted draft. Promote now rebases
on the active doc it was drafted against (`.pending/owner.md.base`).
"""
from core import self_evolution as se
from core.doc_claims import (
    active_rule_lines, split_superseded, strip_stamp, superseded_entries,
)
from core.owner_doc_writer import OwnerDocWriter

UID = "u1"


def _rules(prefix, n):
    return [f"{prefix} {i}: owner said keep rule {prefix} {i}." for i in range(n)]


def _doc(lines):
    return "\n\n".join(lines) + "\n"


def _active_keys(w):
    return {strip_stamp(l).strip() for l in active_rule_lines(w.read_active(UID))}


def test_draft_against_older_baseline_does_not_supersede_live_rules(tmp_path):
    w = OwnerDocWriter(tmp_path)
    old = _rules("OLD", 5)
    assert w.propose(_doc(old), user_id=UID, created_by="user", pending=False).ok
    # The agent drafts a full-body update against the 5-rule doc (adds one rule).
    assert w.propose(_doc(old + ["NEW: owner prefers short replies."]),
                     user_id=UID, created_by="agent").pending
    # Meanwhile 60 rules land in the active doc (another promote / owner edit).
    live = _rules("LIVE", 60)
    assert w.propose(_doc(old + live), user_id=UID, created_by="user", pending=False).ok

    item = w.list_pending(UID)
    assert len(item["keeps"]) == 60 and not item["retires"]
    assert item["preview"].startswith("keeps 60 active rule(s)")

    res = w.promote(user_id=UID)
    assert res.ok and not res.pending
    keys = _active_keys(w)
    assert set(live) <= keys and set(old) <= keys
    assert "NEW: owner prefers short replies." in keys
    assert not superseded_entries(w.read_active(UID))
    assert len(res.kept) == 60 and not res.retired
    assert not w._base_file(UID).exists()


def test_rule_the_draft_retires_is_retired_and_named(tmp_path):
    w = OwnerDocWriter(tmp_path)
    rules = _rules("R", 4)
    assert w.propose(_doc(rules), user_id=UID, created_by="user", pending=False).ok
    assert w.propose(_doc(rules[1:]), user_id=UID, created_by="agent").pending
    assert w.list_pending(UID)["preview"].startswith("retires 1 active rule(s)")
    ok, msg = se.promote(se.KIND_OWNER, UID, user_id=UID, home_dir=tmp_path,
                         instance_id=w.instance_id)
    assert ok and "retires 1 active rule(s)" in msg and "R 0" in msg
    assert rules[0] not in _active_keys(w)
    assert any("R 0" in e for e in superseded_entries(w.read_active(UID)))


def test_many_retirements_are_named_with_count(tmp_path):
    """A legacy draft (no base kept) still wins, but it is never silent."""
    w = OwnerDocWriter(tmp_path)
    live = _rules("LIVE", 60)
    assert w.propose(_doc(live), user_id=UID, created_by="user", pending=False).ok
    pf = w._pending_file(UID)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(_doc(live[:2]), encoding="utf-8")   # queued before .base existed
    assert w.list_pending(UID)["preview"].startswith("retires 58 active rule(s)")
    res = w.promote(user_id=UID)
    assert res.ok and len(res.retired) == 58


def test_line_retired_since_the_draft_stays_retired(tmp_path):
    """The 10-10 case: a stale line the draft only carried does not come back."""
    w = OwnerDocWriter(tmp_path)
    stale = "REBUYS RESUMED: job 0a1b2c3d4e5f runs hourly."
    base = ["Owner timezone: UTC+7.", stale]
    assert w.propose(_doc(base), user_id=UID, created_by="user", pending=False).ok
    assert w.propose(_doc(base + ["Owner prefers short replies."]),
                     user_id=UID, created_by="agent").pending
    # The owner retires the stale line while the draft waits.
    assert w.propose(_doc(base[:1]), user_id=UID, created_by="user", pending=False).ok
    assert w.promote(user_id=UID).ok
    active, _sup = split_superseded(w.read_active(UID))
    assert stale not in active
    assert "Owner prefers short replies." in active


def test_unchanged_baseline_promotes_the_draft_as_written(tmp_path):
    w = OwnerDocWriter(tmp_path)
    assert w.propose("A rule.\n", user_id=UID, created_by="user", pending=False).ok
    assert w.propose("A rule.\n\nB rule.\n", user_id=UID, created_by="agent").pending
    item = w.list_pending(UID)
    assert not item["retires"] and not item["keeps"]
    res = w.promote(user_id=UID)
    assert res.ok and not res.retired and not res.kept
    assert {"A rule.", "B rule."} <= _active_keys(w)


def test_reject_drops_the_base(tmp_path):
    w = OwnerDocWriter(tmp_path)
    assert w.propose("A rule.\n", user_id=UID, created_by="user", pending=False).ok
    assert w.propose("B rule.\n", user_id=UID, created_by="agent").pending
    assert w._base_file(UID).exists()
    assert w.reject(user_id=UID).ok
    assert not w._base_file(UID).exists()
