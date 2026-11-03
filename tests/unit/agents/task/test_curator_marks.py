"""098 AG-13: the curator mark records an effect that happened; a restore
resets it; a failed pass backs off."""
import pytest

from agents.task.agent.core.curator import CURATOR_MARK, SkillCurator
from modules.skills.skill_usage import SkillUsageStore

DAY = 86400.0


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("CURATOR_STALE_DAYS", "30")
    monkeypatch.setenv("CURATOR_ARCHIVE_DAYS", "90")


@pytest.fixture
def usage(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"), clock=lambda: 1_000_000.0)


class _SM:
    def __init__(self, ok):
        self.ok = ok

    def delete_skill(self, skill_id, *, user_id, **_kw):
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok


def _cur(usage, sm, now):
    return SkillCurator(sm, usage, clock=lambda: now,
                        data_dir=str(usage.db_path).rsplit("/", 1)[0])


@pytest.mark.parametrize("result", [False, OSError("disk full")])
def test_failed_archive_does_not_set_mark(usage, result):
    usage.record_provenance("s", "u1", "agent")
    plan = _cur(usage, _SM(result), 1_000_000.0 + 120 * DAY).apply_automatic_transitions()
    assert "u1/s" in plan["archived"]
    assert usage.get_state("curator:u1/s") == "stale"   # not archived: try again next pass


def test_successful_archive_sets_mark(usage):
    usage.record_provenance("s", "u1", "agent")
    _cur(usage, _SM(True), 1_000_000.0 + 120 * DAY).apply_automatic_transitions()
    assert usage.get_state("curator:u1/s") == "archived"


def test_rollback_resets_mark_to_active(tmp_path, monkeypatch):
    from modules.skills import skill_usage as su
    store = SkillUsageStore(str(tmp_path / "su.db"))
    monkeypatch.setattr(su, "get_skill_usage_store", lambda *a, **k: store)
    store.set_state("curator:u1/s", "archived")
    from modules.skills.writer import SkillWriterMixin, SkillWriteResult

    class _W(SkillWriterMixin):
        pass
    w = _W.__new__(_W)
    monkeypatch.setattr(_W, "_require_user", lambda self, u: u, raising=False)
    monkeypatch.setattr(_W, "validate_skill_id", lambda self, i: (True, []), raising=False)
    monkeypatch.setattr(_W, "_bundled_ids", lambda self: set(), raising=False)
    monkeypatch.setattr(_W, "_archived_bodies", lambda self, *a, **k: {"a" * 64: "body"},
                        raising=False)
    monkeypatch.setattr(_W, "_load_user_rules_raw", lambda self, u: {}, raising=False)
    monkeypatch.setattr(_W, "create_skill",
                        lambda self, *a, **k: SkillWriteResult("s", True), raising=False)
    assert w.rollback_skill("s", user_id="u1", to_sha="a" * 64)
    assert store.get_state("curator:u1/s") == "active"


def test_failed_pass_backs_off(usage, monkeypatch):
    now = {"t": 1_000_000.0}
    cur = SkillCurator(_SM(True), usage, clock=lambda: now["t"],
                       data_dir=str(usage.db_path).rsplit("/", 1)[0])

    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(cur, "apply_automatic_transitions", boom)
    monkeypatch.setattr(cur, "contribution_report", lambda: [])
    cur._run_once_blocking()
    assert usage.get_state("curator:failures") == "1"
    now["t"] += 1800           # half an hour: still backing off
    assert cur.should_run() is False
    now["t"] += 3600           # past the first step (1 h)
    assert cur.should_run() is True
    cur._run_once_blocking()
    now["t"] += 3700           # second failure: 2 h
    assert cur.should_run() is False


def test_mark_table_is_well_formed():
    assert CURATOR_MARK.validate() == []
    assert not CURATOR_MARK.allowed("archived", "stale")
