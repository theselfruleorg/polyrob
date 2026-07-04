"""Tests for `polyrob skills export <id> [--to DIR]` (Task 18)."""
from click.testing import CliRunner

from cli.commands.skills import skills


def test_export_copies_skill_folder(tmp_path):
    # a builtin skill with resources exports as a self-contained folder
    dest = tmp_path / "out"
    r = CliRunner().invoke(skills, ["export", "secret-handling", "--to", str(dest)])
    assert r.exit_code == 0, r.output
    exported = dest / "secret-handling" / "SKILL.md"
    assert exported.exists() and "---" in exported.read_text()[:4]   # frontmatter preserved


def test_export_unknown_id_errors(tmp_path):
    r = CliRunner().invoke(skills, ["export", "does-not-exist", "--to", str(tmp_path)])
    assert r.exit_code != 0 and "no such skill" in r.output.lower()


def test_export_refuses_to_clobber_existing_target(tmp_path):
    dest = tmp_path / "out"
    r1 = CliRunner().invoke(skills, ["export", "secret-handling", "--to", str(dest)])
    assert r1.exit_code == 0, r1.output
    r2 = CliRunner().invoke(skills, ["export", "secret-handling", "--to", str(dest)])
    assert r2.exit_code != 0
    assert "already exists" in r2.output.lower() or "exists" in r2.output.lower()
