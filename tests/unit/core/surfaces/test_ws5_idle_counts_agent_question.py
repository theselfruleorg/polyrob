"""2026-10-13 review C3: idle rollover counts the agent's own question to the
owner, not only the owner's last inbound."""
from types import SimpleNamespace

from core.surfaces.session_policy import should_start_fresh, with_agent_activity

H = 3600.0


def test_an_answer_to_the_agents_question_continues_the_session():
    t0 = 1_760_000_000.0
    row = {"updated_at": t0, "last_outbound_at": t0 + 2 * H}   # owner 08:00, agent asks 10:00
    fresh, reason = should_start_fresh(row, now=t0 + 12.5 * H, idle_minutes=720,
                                       daily_hour=4, mode="idle")
    assert (fresh, reason) == (False, "continue")


def test_without_the_question_it_is_still_idle():
    t0 = 1_760_000_000.0
    fresh, reason = should_start_fresh({"updated_at": t0}, now=t0 + 12.5 * H,
                                       idle_minutes=720, daily_hour=4, mode="idle")
    assert (fresh, reason) == (True, "idle")


def test_with_agent_activity_reads_the_owner_thread(tmp_path):
    from core.surfaces.owner_thread import OwnerThreadStore
    store = OwnerThreadStore(str(tmp_path / "owner_thread.db"))
    store.record("out", "u1", "which wallet?", via="telegram", session_id="s1",
                 now=1_760_007_200.0)
    store.record("out", "u1", "other session", via="telegram", session_id="s2",
                 now=1_760_090_000.0)
    import core.surfaces.owner_thread as ot
    orig = ot.resolve_store
    ot.resolve_store = lambda container, create: store
    try:
        row = with_agent_activity({"session_id": "s1", "user_id": "u1",
                                   "updated_at": 1_760_000_000.0}, SimpleNamespace())
    finally:
        ot.resolve_store = orig
    assert row["last_outbound_at"] == 1_760_007_200.0


def test_with_agent_activity_fails_open():
    row = {"session_id": "s1", "user_id": "u1", "updated_at": 1.0}
    assert with_agent_activity(row, None) == row
