"""Task 4 — wire the strict skill-authoring validator into `polyrob skills validate`
and `polyrob doctor`.

`SkillManager.validate_all_authored()` is the new method (Task 4 Step 3): a
dict[str, list[Issue]] over every authored (non-dotted, non-``user_``) skill
directory, keyed by skill id, containing ONLY skills with at least one issue.
"""
from click.testing import CliRunner

from agents.task.agent.skill_manager import get_skill_manager


def test_validate_all_returns_no_errors_for_bundled_library():
    issues = get_skill_manager().validate_all_authored()   # new method, Task 4 Step 3
    errors = [i for skill, ii in issues.items() for i in ii if i.level == "error"]
    assert errors == [], f"bundled skills not compliant: {errors}"


def test_count_authored_skills_matches_bundled_library_size():
    mgr = get_skill_manager()
    # Same predicate validate_all_authored() uses: a directory under skills_dir with
    # a SKILL.md, excluding dotted/``user_``-prefixed dirs.
    on_disk = [
        d for d in mgr.skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists() and not d.name.startswith((".", "user_"))
    ]
    assert mgr.count_authored_skills() == len(on_disk)
    assert mgr.count_authored_skills() == 26  # 26 bundled skills (polyrob-user-guide added 2026-07-12)


def test_cli_skills_validate_no_arg_exits_zero_for_compliant_library():
    import cli.commands.skills as skills_mod

    result = CliRunner().invoke(skills_mod.skills, ["validate"])
    assert result.exit_code == 0, result.output


def test_cli_skills_validate_no_arg_reports_grouped_issues_and_exits_nonzero(monkeypatch):
    import cli.commands.skills as skills_mod
    from agents.task.agent.skill_validation import Issue

    class _FakeMgr:
        def validate_all_authored(self):
            return {"bad-skill": [Issue("error", "missing_name", "name is required")]}

    monkeypatch.setattr(skills_mod, "get_skill_manager", lambda: _FakeMgr())
    result = CliRunner().invoke(skills_mod.skills, ["validate"])
    assert result.exit_code == 1
    assert "bad-skill" in result.output
    assert "missing_name" in result.output


def test_cli_skills_validate_no_arg_all_compliant_message(monkeypatch):
    import cli.commands.skills as skills_mod

    class _FakeMgr:
        def validate_all_authored(self):
            return {}

    monkeypatch.setattr(skills_mod, "get_skill_manager", lambda: _FakeMgr())
    result = CliRunner().invoke(skills_mod.skills, ["validate"])
    assert result.exit_code == 0, result.output


def test_doctor_report_shows_skills_compliance_line():
    from cli.commands.doctor import doctor_report

    lines = doctor_report({})
    blob = "\n".join(lines)
    assert "compliant" in blob.lower()
    assert "26" in blob  # all 26 bundled skills currently compliant
