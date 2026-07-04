"""Task 24 — skill install audit trail (`skill_install_audit` table).

Records source/resolved-sha/approver/ts for every `polyrob skill install`/
`approve` so an operator can answer "where did this active skill come from,
who approved it, and when" (P2 exit criteria: "audited").
"""
import pytest

from modules.skills.skill_usage import SkillUsageStore


@pytest.fixture
def store(tmp_path):
    return SkillUsageStore(str(tmp_path / "skill_usage.db"))


def test_install_audit_row_written(store):
    store.record_install(
        "myskill", user_id="7", source="git:anthropics/skills/pdf",
        resolved_sha="abc1234", approver="7",
    )
    rows = store.list_installs(user_id="7")
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "myskill"
    assert r["source"].startswith("git:")
    assert r["resolved_sha"] == "abc1234"
    assert r["approver"] == "7"
    assert r["ts"] > 0


def test_install_audit_resolved_sha_optional(store):
    store.record_install("local-skill", user_id="7", source="local", approver="7")
    rows = store.list_installs(user_id="7")
    assert rows[0]["resolved_sha"] is None


def test_install_audit_filters_by_user_id(store):
    store.record_install("s1", user_id="7", source="local", approver="7")
    store.record_install("s2", user_id="9", source="local", approver="9")
    assert len(store.list_installs(user_id="7")) == 1
    assert len(store.list_installs(user_id="9")) == 1
    assert len(store.list_installs()) == 2  # no filter -> all tenants


def test_install_audit_explicit_ts_and_ordering(tmp_path):
    store = SkillUsageStore(str(tmp_path / "u.db"))
    store.record_install("older", user_id="7", source="local", approver="7", ts=100.0)
    store.record_install("newer", user_id="7", source="local", approver="7", ts=200.0)
    rows = store.list_installs(user_id="7")
    assert [r["name"] for r in rows] == ["newer", "older"]
