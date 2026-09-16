"""044 T20: `cron.room_service.service` — the ONE owner-seat helper behind
`/groups service` and `polyrob owner groups service`.

It CREATES a durable recurring cron job (the room's cadence), is idempotent per
room, and `off` stops it. It lives in the cron tier rather than in
`core/surfaces/group_admin.py` because core may not import cron (layering
ratchet); the chat-id normalization is shared with group_admin so both modules
always resolve the same room.
"""
import types

from cron.jobs import CronJobStore
from cron.room_service import service
from cron.service import CronService


class _C:
    def __init__(self, tmp_path, *, allow=("-1", "-2", "-1001", "100")):
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))
        from core.surfaces.group_allowlist import GroupAllowlist
        al = GroupAllowlist(str(tmp_path / "group_allowlist.db"))
        for chat_id in allow:
            al.allow("telegram", chat_id)

    def get_service(self, n):
        return None


def _jobs(tmp_path, user_id="rob"):
    return CronService(CronJobStore(str(tmp_path / "cron.db"))).list_jobs(user_id)


def test_service_creates_a_recurring_cron_job(tmp_path):
    out = service(_C(tmp_path), "rob", "telegram", "-1", every="1h", max_replies=5)
    assert "1h" in out and "telegram:-1" in out
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.schedule_spec == "1h" and job.one_shot is False
    assert job.payload["group"] == {"surface": "telegram", "chat_id": "-1",
                                    "chat_type": "supergroup"}
    assert job.payload["max_replies"] == 5
    assert job.id[:8] in out, "the owner must be told the job id it can cancel"


def test_service_is_idempotent_per_room(tmp_path):
    """A second `/groups service here` must not leave two jobs answering the
    same room on the same cadence."""
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    out = service(c, "rob", "telegram", "-1")
    assert "already" in out.lower()
    assert len(_jobs(tmp_path)) == 1


def test_another_room_gets_its_own_job(tmp_path):
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    service(c, "rob", "telegram", "-2")
    assert len(_jobs(tmp_path)) == 2


def test_service_off_cancels_the_job(tmp_path):
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    out = service(c, "rob", "telegram", "-1", every="off")
    assert "stopped" in out.lower()
    assert [j.status for j in _jobs(tmp_path)] == ["cancelled"]
    # And it can be started again afterwards.
    service(c, "rob", "telegram", "-1")
    assert sorted(j.status for j in _jobs(tmp_path)) == ["cancelled", "scheduled"]


def test_service_off_with_no_job_says_so(tmp_path):
    out = service(_C(tmp_path), "rob", "telegram", "-1", every="off")
    assert "no service job" in out.lower()


def test_service_rejects_an_unparseable_cadence(tmp_path):
    out = service(_C(tmp_path), "rob", "telegram", "-1", every="whenever")
    assert out.startswith("❌")
    assert _jobs(tmp_path) == []


def test_service_normalizes_the_underscore_chat_alias(tmp_path):
    """`_100…` and `-100…` must be the SAME room, or the CLI alias would create
    a second job that services a chat id nothing else uses."""
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1001")
    out = service(c, "rob", "telegram", "_1001")
    assert "already" in out.lower()
    assert len(_jobs(tmp_path)) == 1


def test_another_tenants_job_is_invisible(tmp_path):
    """Job lookup is tenant-scoped: a second owner must not be told his room is
    already serviced because somebody else's is."""
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    out = service(c, "other", "telegram", "-1")
    assert "already" not in out.lower()
    assert len(_jobs(tmp_path, "other")) == 1


def test_service_refuses_a_room_the_agent_is_not_in(tmp_path):
    """Minor 10: binding a job to a chat the agent was never allowed into (or was
    removed from) produces a job that reads an empty ledger and skips forever."""
    c = _C(tmp_path, allow=())
    out = service(c, "rob", "telegram", "-1")
    assert out.startswith("\u274c") and "not an allowed room" in out
    assert _jobs(tmp_path) == []


def test_off_still_works_for_a_room_that_was_since_denied(tmp_path):
    from core.surfaces.group_allowlist import GroupAllowlist
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).revoke("telegram", "-1")
    assert "stopped" in service(c, "rob", "telegram", "-1", every="off").lower()


# ---------------------------------------------------------------------------
# Fix round 2
# ---------------------------------------------------------------------------

def test_a_left_room_is_refused_even_when_a_job_exists(tmp_path):
    """Obs 2: the allowlist check ran AFTER the existing-job branch, so a room
    the bot had been kicked from answered "Already servicing" — a confident
    sentence about a room the agent cannot reach."""
    from core.surfaces.group_allowlist import GroupAllowlist
    c = _C(tmp_path)
    service(c, "rob", "telegram", "-1")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).mark_left("telegram", "-1")
    out = service(c, "rob", "telegram", "-1")
    assert "not an allowed room" in out
    assert "still scheduled" in out, "the owner must be told the job is still there"
    assert "Already servicing" not in out


def test_an_unreadable_allowlist_is_a_sentence(tmp_path, monkeypatch):
    """N3: every failure of this verb is an `❌ …` sentence, never a traceback."""
    from core.surfaces import group_admin
    monkeypatch.setattr(group_admin, "is_room_allowed",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db gone")))
    out = service(_C(tmp_path), "rob", "telegram", "-1")
    assert out.startswith("❌") and "db gone" in out


def test_the_reply_noun_agrees(tmp_path):
    """Obs 4: "At most 1 replies" is exactly the sloppiness a model imitates."""
    c = _C(tmp_path)
    assert "1 reply per run" in service(c, "rob", "telegram", "-1", max_replies=1)
    assert "3 replies per run" in service(c, "rob", "telegram", "-2", max_replies=3)
