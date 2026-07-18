"""Self-evolution transparency loop (§7.1) — the owner-facing aggregator over the
two pending→promote pipelines (self-context + authored skills).

list_pending unifies both; promote/reject dispatch by (kind, id); the notification
builder turns a pending set into one owner message. All owner-gated at the caller.
"""
from agents.task.agent.skill_manager import SkillManager
from core.self_context_writer import SelfContextWriter, PROVENANCE_AGENT
from core import self_evolution

GOOD_SKILL = "# My Skill\n\nWhen X, do Y. A useful reusable procedure with enough text.\n"


def _seed_self_pending(home, uid="gleb"):
    SelfContextWriter(home, instance_id="rob").propose(
        "Learned: surface blockers to the owner proactively.",
        user_id=uid, created_by=PROVENANCE_AGENT, pending=True)


def _skill_mgr(home):
    return SkillManager(skills_dir=home / "skills")


# --- list_pending aggregation ------------------------------------------------

def test_list_pending_empty(tmp_path):
    items = self_evolution.list_pending(
        "gleb", home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert items == []


def test_list_pending_includes_self_context(tmp_path):
    _seed_self_pending(tmp_path)
    items = self_evolution.list_pending(
        "gleb", home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert len(items) == 1
    assert items[0]["kind"] == "self_context"
    assert "surface blockers" in items[0]["preview"]


def test_list_pending_includes_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    mgr = _skill_mgr(tmp_path)
    mgr.create_skill("learned-thing", GOOD_SKILL, user_id="gleb", created_by="agent")
    items = self_evolution.list_pending(
        "gleb", home_dir=tmp_path, instance_id="rob", skill_manager=mgr)
    kinds = {i["kind"] for i in items}
    assert "skill" in kinds
    skill_item = next(i for i in items if i["kind"] == "skill")
    assert skill_item["id"] == "learned-thing"


def test_list_pending_tenant_scoped(tmp_path):
    _seed_self_pending(tmp_path, uid="gleb")
    items = self_evolution.list_pending(
        "mallory", home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert items == []


# --- promote / reject dispatch ----------------------------------------------

def test_promote_self_context(tmp_path):
    from core.instance import load_self_doc
    _seed_self_pending(tmp_path)
    ok, _ = self_evolution.promote(
        "self_context", "gleb", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert ok
    assert "surface blockers" in load_self_doc(tmp_path, user_id="gleb")


def test_reject_self_context(tmp_path):
    _seed_self_pending(tmp_path)
    ok, _ = self_evolution.reject(
        "self_context", "gleb", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert ok
    remaining = self_evolution.list_pending(
        "gleb", home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert remaining == []


def test_promote_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    mgr = _skill_mgr(tmp_path)
    mgr.create_skill("learned-thing", GOOD_SKILL, user_id="gleb", created_by="agent")
    ok, _ = self_evolution.promote(
        "skill", "learned-thing", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=mgr)
    assert ok
    assert "learned-thing" in getattr(mgr, "skill_rules", {}) or \
        (mgr._user_root("gleb") / "learned-thing" / "SKILL.md").exists()


def test_reject_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    mgr = _skill_mgr(tmp_path)
    mgr.create_skill("bad-thing", GOOD_SKILL, user_id="gleb", created_by="agent")
    ok, _ = self_evolution.reject(
        "skill", "bad-thing", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=mgr)
    assert ok
    assert mgr.list_pending_skills(user_id="gleb") == []


def test_promote_unknown_kind_errors(tmp_path):
    ok, msg = self_evolution.promote(
        "bogus", "x", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=_skill_mgr(tmp_path))
    assert not ok


# --- notification builder ----------------------------------------------------

def test_notification_none_when_empty():
    assert self_evolution.build_pending_notification([]) is None


def test_notification_summarizes_items():
    items = [
        {"kind": "self_context", "id": "gleb", "preview": "surface blockers proactively"},
        {"kind": "skill", "id": "learned-thing", "preview": "when X do Y"},
    ]
    msg = self_evolution.build_pending_notification(items)
    assert msg is not None
    assert "learned-thing" in msg
    assert "surface blockers" in msg
    # actionable: mentions how to approve
    assert "approve" in msg.lower()


# --- flag --------------------------------------------------------------------

def test_flag_default_off_on_server(monkeypatch):
    monkeypatch.delenv("SELF_EVOLUTION_TRANSPARENCY", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.self_evolution_transparency() is False


def test_flag_on_under_local(monkeypatch):
    monkeypatch.delenv("SELF_EVOLUTION_TRANSPARENCY", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.self_evolution_transparency() is True


# --- proactive owner notification (fail-open) --------------------------------

import pytest


class _FakeSink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _FakeContainer:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return self._sink if name in ("telegram_sink", "message_router") else None


@pytest.mark.asyncio
async def test_notify_owner_pending_sends_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    _seed_self_pending(tmp_path)
    sink = _FakeSink()
    ok = await self_evolution.maybe_notify_owner_pending(
        _FakeContainer(sink), "gleb", home_dir=tmp_path, instance_id="rob",
        skill_manager=_skill_mgr(tmp_path))
    assert ok
    assert sink.sent and "surface blockers" in sink.sent[0][1]


@pytest.mark.asyncio
async def test_notify_owner_pending_noop_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "false")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    _seed_self_pending(tmp_path)
    sink = _FakeSink()
    ok = await self_evolution.maybe_notify_owner_pending(
        _FakeContainer(sink), "gleb", home_dir=tmp_path, instance_id="rob",
        skill_manager=_skill_mgr(tmp_path))
    assert ok is False
    assert sink.sent == []


@pytest.mark.asyncio
async def test_notify_owner_pending_failopen_no_container(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    ok = await self_evolution.maybe_notify_owner_pending(
        None, "gleb", home_dir=tmp_path, instance_id="rob")
    assert ok is False


# --- T4-04 (+§3.2): pushes ride the ONE delivery rail; an undeliverable push
# persists a durable owner_notice ---------------------------------------------

@pytest.mark.asyncio
async def test_push_owner_message_records_notice_when_no_sink(monkeypatch):
    import core.self_evolution as se
    import core.surfaces.user_delivery as ud
    calls = []
    monkeypatch.setattr(ud, "_record_notice",
                        lambda event_log, user_id, text: calls.append(text))

    class _NoSink:
        def get_service(self, name):
            return None  # REPL/local owner: no telegram sink registered

    ok = await se.push_owner_message(_NoSink(), "I'm blocked; grant twitter access")
    assert ok is False  # not delivered live
    assert calls == ["I'm blocked; grant twitter access"]  # but not lost


@pytest.mark.asyncio
async def test_push_owner_message_no_notice_when_delivered(monkeypatch):
    import core.self_evolution as se
    import core.surfaces.user_delivery as ud
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    calls = []
    monkeypatch.setattr(ud, "_record_notice",
                        lambda event_log, user_id, text: calls.append(text))

    class _Sink:
        async def send_message(self, chat_id, text):
            return True

    class _C:
        def get_service(self, name):
            return _Sink() if name in ("telegram_sink", "message_router") else None

    ok = await se.push_owner_message(_C(), "hi owner")
    assert ok is True
    assert calls == []  # delivered live -> no fallback notice


@pytest.mark.asyncio
async def test_push_owner_message_dedups_identical_content(monkeypatch):
    """§3.2: the shared push rail now has a memory — the same text twice within
    the dedup window sends once (the watermark duplicate-spam class)."""
    import core.self_evolution as se
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    sent = []

    class _Sink:
        async def send_message(self, chat_id, text):
            sent.append(text)
            return True

    class _C:
        def get_service(self, name):
            return _Sink() if name == "telegram_sink" else None

    c = _C()
    assert await se.push_owner_message(c, "✅ goal done. Result: X") is True
    assert await se.push_owner_message(c, "✅ goal done. Result: X") is False
    assert sent == ["✅ goal done. Result: X"]


# --- 019 #1: pending-notification batching (fingerprint cooldown) -------------
# maybe_notify_owner_pending must NOT re-attempt a delivery while the pending
# set is unchanged since the last notification (29/30 daily slots were burned
# by it on 2026-07-18, starving the daily digest). A genuinely new pending
# item, an owner promote/reject, or a process restart re-notifies.


def _patch_rail(monkeypatch, outcomes=None):
    """Count delivery ATTEMPTS entering the rail. Bypasses the real rail so the
    fingerprint batching is tested independently of the rail's own
    content-hash dedup (which would also suppress an identical second text)."""
    import core.surfaces.user_delivery as ud
    attempts = []
    queue = list(outcomes or [])

    async def _fake_deliver(container, user_id, text, **kw):
        attempts.append(text)
        return queue.pop(0) if queue else "sent"

    monkeypatch.setattr(ud, "deliver_user_message", _fake_deliver)
    return attempts


async def _notify(container, tmp_path, mgr):
    return await self_evolution.maybe_notify_owner_pending(
        container, "gleb", home_dir=tmp_path, instance_id="rob",
        skill_manager=mgr)


@pytest.mark.asyncio
async def test_notify_unchanged_pending_set_attempts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    attempts = _patch_rail(monkeypatch)
    _seed_self_pending(tmp_path)
    mgr = _skill_mgr(tmp_path)
    c = object()  # non-None is all the patched rail needs
    assert await _notify(c, tmp_path, mgr) is True
    assert await _notify(c, tmp_path, mgr) is False  # unchanged set: no re-notify
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_notify_new_pending_item_notifies_again(tmp_path, monkeypatch):
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    monkeypatch.setenv("SKILLS_WRITABLE_REQUIRE_REVIEW", "true")
    attempts = _patch_rail(monkeypatch)
    _seed_self_pending(tmp_path)
    mgr = _skill_mgr(tmp_path)
    c = object()
    assert await _notify(c, tmp_path, mgr) is True
    assert await _notify(c, tmp_path, mgr) is False
    # a genuinely NEW pending item changes the set -> prompt notify
    mgr.create_skill("learned-thing", GOOD_SKILL, user_id="gleb", created_by="agent")
    assert await _notify(c, tmp_path, mgr) is True
    assert len(attempts) == 2
    assert "learned-thing" in attempts[-1]


@pytest.mark.asyncio
async def test_notify_fingerprint_survives_restart(tmp_path, monkeypatch):
    """The fingerprint is persisted under the identity tier root, so a process
    restart (module state gone) does not re-notify an unchanged set."""
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    attempts = _patch_rail(monkeypatch)
    _seed_self_pending(tmp_path)
    mgr = _skill_mgr(tmp_path)
    c = object()
    assert await _notify(c, tmp_path, mgr) is True
    self_evolution._notify_fingerprints.clear()  # simulate restart
    assert await _notify(c, tmp_path, mgr) is False
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_notify_after_promote_notifies_reproposal(tmp_path, monkeypatch):
    """An owner promote clears the fingerprint: a re-proposal under the SAME
    kind/id (fresh self-context draft) must notify again."""
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    attempts = _patch_rail(monkeypatch)
    _seed_self_pending(tmp_path)
    mgr = _skill_mgr(tmp_path)
    c = object()
    assert await _notify(c, tmp_path, mgr) is True
    ok, _ = self_evolution.promote(
        "self_context", "gleb", user_id="gleb",
        home_dir=tmp_path, instance_id="rob", skill_manager=mgr)
    assert ok
    _seed_self_pending(tmp_path)  # same kind:id as before
    assert await _notify(c, tmp_path, mgr) is True
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_notify_rate_limited_attempt_is_retried(tmp_path, monkeypatch):
    """A rate_limited outcome wrote nothing durable, so the set does NOT count
    as notified — the next pending write retries."""
    monkeypatch.setenv("SELF_EVOLUTION_TRANSPARENCY", "true")
    attempts = _patch_rail(monkeypatch, outcomes=["rate_limited", "sent"])
    _seed_self_pending(tmp_path)
    mgr = _skill_mgr(tmp_path)
    c = object()
    assert await _notify(c, tmp_path, mgr) is False  # rate_limited
    assert await _notify(c, tmp_path, mgr) is True   # retried and delivered
    assert await _notify(c, tmp_path, mgr) is False  # now batched
    assert len(attempts) == 2
