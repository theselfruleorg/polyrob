"""035 P0-1 / P0-5 — the pending-notification state and conflict detection.

The 2026-09-08 incident: the notifier keyed on a CONTENT-INDEPENDENT set of
``kind:id`` pairs, and the identity docs are single-slot (``id`` == tenant). Once
every slot was occupied, a REVISED draft produced the identical fingerprint and
returned before attempting delivery — so four "stop posting to the den" owner
directives could never notify. A ``capped`` (suppressed) push had also recorded
the set as notified, making the lock permanent.
"""
import json

from core.instance import DEFAULT_INSTANCE_ID
from core.self_context_writer import SelfContextWriter, PROVENANCE_AGENT
from core import self_evolution as se


def _pending(home, text, uid="alice"):
    SelfContextWriter(home, instance_id=DEFAULT_INSTANCE_ID).propose(
        text, user_id=uid, created_by=PROVENANCE_AGENT, pending=True)


def _items(home, uid="alice"):
    return se.list_pending(uid, home_dir=home, instance_id=DEFAULT_INSTANCE_ID,
                           skill_manager=None)


# --- P0-1: per-item content state, not a set fingerprint --------------------

def test_revised_draft_needs_notification(tmp_path):
    """THE incident: same kind:id, different content => must notify again."""
    _pending(tmp_path, "Learned: surface blockers to the owner proactively.")
    first = _items(tmp_path)
    state = se.pending_notify_state(first)
    assert se.pending_needs_notification(first, {}) is True
    assert se.pending_needs_notification(first, state) is False, \
        "an unchanged draft must NOT re-notify (019 anti-spam intent preserved)"

    _pending(tmp_path, "NO-DEN Rule: STOP ALL posting to the Telegram public den.")
    revised = _items(tmp_path)
    assert [i["id"] for i in revised] == [i["id"] for i in first], \
        "single-slot kind: the kind:id set is unchanged by a revision"
    assert se.pending_needs_notification(revised, state) is True, \
        "a REVISED draft must re-notify even though kind:id did not change"


def test_new_item_needs_notification(tmp_path):
    _pending(tmp_path, "a durable note about how the owner works with me")
    items = _items(tmp_path)
    state = se.pending_notify_state(items)
    extra = items + [{"kind": "skill", "id": "new-skill", "preview": "x",
                      "chars": 1, "path": str(tmp_path / "nope.md")}]
    assert se.pending_needs_notification(extra, state) is True


def test_state_roundtrip_and_clear(tmp_path):
    _pending(tmp_path, "a durable note about how the owner works with me")
    items = _items(tmp_path)
    se.save_notified_state("alice", se.pending_notify_state(items),
                           home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    loaded = se.load_notified_state("alice", home_dir=tmp_path,
                                   instance_id=DEFAULT_INSTANCE_ID)
    assert se.pending_needs_notification(items, loaded) is False

    se.clear_notified_item("alice", "self_context", "alice",
                           home_dir=tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    loaded2 = se.load_notified_state("alice", home_dir=tmp_path,
                                    instance_id=DEFAULT_INSTANCE_ID)
    assert se.pending_needs_notification(items, loaded2) is True, \
        "after the owner acts, a re-proposal under the same id must notify"


def test_legacy_fingerprint_file_is_retired_not_trusted(tmp_path):
    """The stuck prod state must recover: the legacy opaque fingerprint file
    can never suppress a notification, so an upgrade re-notifies exactly once."""
    root = se._tier_root("alice", tmp_path, DEFAULT_INSTANCE_ID)
    root.mkdir(parents=True, exist_ok=True)
    (root / se._LEGACY_FINGERPRINT_FILE).write_text("32db30bfd9b31eb4")
    _pending(tmp_path, "NO-DEN Rule: STOP ALL posting to the Telegram public den.")
    items = _items(tmp_path)
    state = se.load_notified_state("alice", home_dir=tmp_path,
                                   instance_id=DEFAULT_INSTANCE_ID)
    assert se.pending_needs_notification(items, state) is True


def test_suppressed_outcome_is_not_delivery():
    """P0-1: a push the owner never saw must not count as notified."""
    assert "capped" not in se._NOTIFIED_OUTCOMES
    assert "quiet_held" not in se._NOTIFIED_OUTCOMES
    assert "sent" in se._NOTIFIED_OUTCOMES


# --- P0-5: a pending rule that contradicts an active one -------------------

def test_detect_conflicts_finds_the_den_case():
    active = ("# Rob — Publishing Policy\n"
              "- X posting: quality over volume. Telegram den = public-facing content only.\n")
    pending = ("## NO-DEN Rule (owner directive) — HIGHEST PRIORITY\n"
               "- STOP ALL posting to the Telegram public den, effective immediately.\n")
    conflicts = se.detect_conflicts(pending, active)
    assert conflicts, "the den prohibition contradicts the active licence"
    assert any("den" in c.lower() for c in conflicts)


def test_detect_conflicts_quiet_when_no_overlap():
    active = "- Daily digest: what I did, earned, spent.\n"
    pending = "- STOP ALL posting to the Telegram public den.\n"
    assert se.detect_conflicts(pending, active) == []


def test_detect_conflicts_ignores_agreeing_prohibitions():
    active = "- Never post to the Telegram public den.\n"
    pending = "- STOP ALL posting to the Telegram public den.\n"
    assert se.detect_conflicts(pending, active) == [], \
        "two prohibitions about the same thing agree; that is not a conflict"


def test_detect_conflicts_handles_empty():
    assert se.detect_conflicts("", "anything") == []
    assert se.detect_conflicts("STOP posting to the den", "") == []


def test_list_pending_surfaces_conflicts(tmp_path):
    SelfContextWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID).propose(
        "Telegram den = public-facing content only. Promote in the public den.",
        user_id="alice", created_by="owner", pending=False)
    _pending(tmp_path, "NO-DEN Rule: STOP ALL posting to the Telegram public den.")
    item = next(i for i in _items(tmp_path) if i["kind"] == "self_context")
    assert item.get("conflicts"), "list_pending must expose the contradiction"


def test_notification_names_the_conflict(tmp_path):
    items = [{"kind": "self_context", "id": "rob", "preview": "NO-DEN Rule …",
              "chars": 40, "path": "/x",
              "conflicts": ["active doc says: Telegram den = public-facing content only"]}]
    msg = se.build_pending_notification(items)
    assert "conflict" in msg.lower()


# --- P0-2: the approval prompt is not lifecycle chatter --------------------

def test_pending_approval_is_a_critical_delivery_source():
    """On 09-08 this message rode source="self_evolution" — the lifecycle
    bucket shared with "▶ goal started" pings — and was dropped by the cap
    40 minutes after seven of those pings exhausted it."""
    import core.surfaces.user_delivery as ud
    assert se.NOTIFY_SOURCE in ud._CRITICAL_SOURCES
    assert se.NOTIFY_SOURCE not in ud._LIFECYCLE_SOURCES
    assert ud.resolve_priority(se.NOTIFY_SOURCE, None) == ud.PRIORITY_CRITICAL


# --- P1-10: approve/reject everything at once ------------------------------

def test_decide_all_promotes_every_pending_item(tmp_path):
    from agents.task.agent.skill_manager import SkillManager
    import os
    os.environ["SKILLS_WRITABLE_REQUIRE_REVIEW"] = "true"
    mgr = SkillManager(skills_dir=tmp_path / "skills")
    mgr.create_skill("learned-thing",
                     "# My Skill\n\nWhen X, do Y. A useful reusable procedure with text.\n",
                     user_id="alice", created_by="agent")
    _pending(tmp_path, "Learned: surface blockers to the owner proactively.")
    assert len(_items_all(tmp_path, mgr)) == 2

    ok, failed, msgs = se.decide_all(True, user_id="alice", home_dir=tmp_path,
                                     instance_id=DEFAULT_INSTANCE_ID, skill_manager=mgr)
    assert ok == 2 and failed == 0, msgs
    assert _items_all(tmp_path, mgr) == [], "the queue must be empty afterwards"


def test_decide_all_on_empty_queue_is_not_an_error(tmp_path):
    ok, failed, msgs = se.decide_all(True, user_id="alice", home_dir=tmp_path,
                                     instance_id=DEFAULT_INSTANCE_ID, skill_manager=None)
    assert (ok, failed) == (0, 0)


def test_decide_all_can_reject(tmp_path):
    _pending(tmp_path, "Learned: surface blockers to the owner proactively.")
    ok, failed, _ = se.decide_all(False, user_id="alice", home_dir=tmp_path,
                                  instance_id=DEFAULT_INSTANCE_ID, skill_manager=None)
    assert ok == 1 and failed == 0
    assert _items(tmp_path) == []


def _items_all(home, mgr, uid="alice"):
    return se.list_pending(uid, home_dir=home, instance_id=DEFAULT_INSTANCE_ID,
                           skill_manager=mgr)
