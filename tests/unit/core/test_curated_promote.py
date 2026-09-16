"""A27 (043) — a pending curated note is promotable from the owner seat.

A forged/autonomous turn quarantines a curated note (``status='pending'`` in the
``curated_memory`` store). Before A27 it was visible and promotable from NO seat:
``self_evolution.promote`` returned "unknown kind". These tests pin the fix:

- the curated store's ``note_update`` accepts a ``status=`` kwarg (promote/archive),
- ``self_evolution.list_pending`` surfaces the pending note (the "visible" half),
- ``self_evolution.promote`` flips it to ``active`` and emits a ``self_modification``,
- ``self_evolution.reject`` archives it,
- a read never CREATES ``memory.db`` (status SSOT rule).
"""
import asyncio
import os

import pytest

from agents.task.agent.skill_manager import SkillManager
from core import self_evolution
from core.instance import DEFAULT_INSTANCE_ID
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

UID = "alice"
KIND = self_evolution.KIND_CURATED_NOTE


def _mgr(home):
    # Hermetic skill store so list_pending never touches the real skills dir.
    return SkillManager(skills_dir=home / "skills")


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Named tenant, no anon-block concerns; keep the caps generous.
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    monkeypatch.setenv("MEMORY_TOOL_MAX_ENTRIES", "50")
    monkeypatch.setenv("MEMORY_TOOL_MAX_CHARS", "2000")
    return tmp_path


def _provider(home):
    return SqliteMemoryProvider(str(home / "memory.db"))


def _seed_pending(home, content="a fact the agent learned overnight",
                  *, title=None, user_id=UID):
    prov = _provider(home)
    nid = asyncio.run(prov.note_create(
        user_id, content, title=title, created_by="background_review",
        status="pending"))
    assert nid is not None
    return nid


def _statuses(home, user_id=UID):
    prov = _provider(home)
    out = {}
    for st in ("pending", "active", "archived"):
        rows = asyncio.run(prov.note_list(user_id, status=st))
        for r in rows:
            out[r["id"]] = st
    return out


# --- the store's status kwarg (A27) -----------------------------------------

def test_note_update_status_promotes(home):
    nid = _seed_pending(home)
    prov = _provider(home)
    assert asyncio.run(prov.note_update(UID, nid, status="active")) is True
    assert _statuses(home).get(nid) == "active"


def test_note_update_status_rejects_unknown_value(home):
    nid = _seed_pending(home)
    prov = _provider(home)
    assert asyncio.run(prov.note_update(UID, nid, status="bogus")) is False
    assert _statuses(home).get(nid) == "pending"


def test_note_update_status_tenant_scoped(home):
    nid = _seed_pending(home, user_id=UID)
    prov = _provider(home)
    # Another tenant cannot promote it.
    assert asyncio.run(prov.note_update("mallory", nid, status="active")) is False
    assert _statuses(home).get(nid) == "pending"


# --- the self_evolution pipeline (A27) --------------------------------------

def test_pending_note_is_visible(home):
    nid = _seed_pending(home, content="remember to renew the cert", title="ops")
    items = self_evolution.list_pending(
        UID, home_dir=home, instance_id=DEFAULT_INSTANCE_ID,
        skill_manager=_mgr(home))
    curated = [i for i in items if i["kind"] == KIND]
    assert len(curated) == 1
    assert curated[0]["id"] == str(nid)
    assert "renew the cert" in curated[0]["preview"]
    assert curated[0]["path"] is None


def test_promote_activates_and_emits(home, monkeypatch):
    nid = _seed_pending(home)
    calls = []
    import core.self_events as se
    monkeypatch.setattr(se, "emit_self_modification",
                        lambda **kw: calls.append(kw))

    ok, msg = self_evolution.promote(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is True
    assert str(nid) in msg
    assert _statuses(home).get(nid) == "active"
    # It no longer shows as pending.
    items = self_evolution.list_pending(
        UID, home_dir=home, instance_id=DEFAULT_INSTANCE_ID,
        skill_manager=_mgr(home))
    assert not [i for i in items if i["kind"] == KIND]
    # The self_modification event fired.
    assert calls and calls[-1]["kind"] == KIND
    assert calls[-1]["action"] == "promote"
    assert calls[-1]["ok"] is True


def test_reject_archives(home):
    nid = _seed_pending(home)
    ok, msg = self_evolution.reject(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is True
    assert _statuses(home).get(nid) == "archived"


def test_promote_missing_note_is_honest(home):
    # memory.db exists (a pending note was seeded), but the id is unknown.
    _seed_pending(home)
    ok, msg = self_evolution.promote(
        KIND, "999999", user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False
    assert "999999" in msg


def test_show_reads_the_body(home):
    nid = _seed_pending(home, content="the full note body", title="head")
    ok, body = self_evolution.show(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID, skill_manager=_mgr(home))
    assert ok is True
    assert "the full note body" in body


def test_read_never_creates_memory_db(home):
    # No note seeded -> no memory.db -> a list read must not create the store.
    assert not os.path.exists(home / "memory.db")
    items = self_evolution.list_pending(
        UID, home_dir=home, instance_id=DEFAULT_INSTANCE_ID,
        skill_manager=_mgr(home))
    assert [i for i in items if i["kind"] == KIND] == []
    assert not os.path.exists(home / "memory.db")


def test_decide_unknown_when_no_store(home):
    # Promote against a home with no memory.db -> honest miss, no crash.
    ok, msg = self_evolution.promote(
        KIND, "1", user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False


# --- state-integrity + core owner-decide tenant scoping (review H5) ----------

def test_reject_of_already_active_note_is_a_noop(home):
    # Minor 1: the owner-decide path transitions only a PENDING note. A reject
    # (or promote) aimed at an already-ACTIVE id is a refusal, never a silent
    # flip to archived.
    nid = _seed_pending(home)
    ok, _ = self_evolution.promote(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is True
    assert _statuses(home).get(nid) == "active"

    ok, msg = self_evolution.reject(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False
    assert str(nid) in msg
    # The active note is untouched — not archived.
    assert _statuses(home).get(nid) == "active"

    # A second promote of the now-active note is likewise a no-op.
    ok, _ = self_evolution.promote(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False
    assert _statuses(home).get(nid) == "active"


def test_core_owner_decide_tenant_scoped(home):
    # Minor 2: pin the CORE owner-decide tenant scoping (not just the store
    # path). Another tenant cannot promote or reject alice's note, and it stays
    # pending for the rightful owner.
    nid = _seed_pending(home, user_id=UID)

    ok, _ = self_evolution.promote(
        KIND, str(nid), user_id="mallory", home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False
    assert _statuses(home).get(nid) == "pending"

    ok, _ = self_evolution.reject(
        KIND, str(nid), user_id="mallory", home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is False
    assert _statuses(home).get(nid) == "pending"

    # The rightful owner still can.
    ok, _ = self_evolution.promote(
        KIND, str(nid), user_id=UID, home_dir=home,
        instance_id=DEFAULT_INSTANCE_ID)
    assert ok is True
    assert _statuses(home).get(nid) == "active"
