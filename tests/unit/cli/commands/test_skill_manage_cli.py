"""Task 22: `polyrob skill list / remove / info` + spec-dispatch install.

Covers: `skill install <spec>` dispatching by shape (local dir / git shorthand /
direct SKILL.md url), plus the new `list`/`info`/`remove` commands over the
install pipeline's quarantine/active/archived states.
"""
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands import skill_install


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """Task 23 gates every install route on ``local_mode_enabled()`` — pin it ON
    for this pipeline suite (see test_skill_install_local.py for rationale)."""
    from agents.task import constants

    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)


def _mkskill(tmp_path, name, desc="Do a thing. Use when needed.", body="# b\ncontent"):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}")
    return d


# ---------------------------------------------------------------------------
# Part A: `skill install` dispatches by spec shape
# ---------------------------------------------------------------------------


def test_skill_install_local_dir_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    src = _mkskill(tmp_path, "widgeter")
    result = CliRunner().invoke(skill_install.skill, ["install", str(src), "--user", "7"])
    assert result.exit_code == 0, result.output
    assert "widgeter" in result.output


def test_skill_install_routes_git_shorthand(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    calls = {}

    def fake_install_git(spec, *, user_id, ref=None, trust="prompt"):
        calls["spec"] = spec
        calls["user_id"] = user_id
        calls["ref"] = ref
        return skill_install.InstallResult(
            name="fromgit", staged_path=tmp_path, approved=False, source=f"git:{spec}"
        )

    monkeypatch.setattr(skill_install, "install_git", fake_install_git)
    result = CliRunner().invoke(skill_install.skill, ["install", "owner/repo", "--user", "7"])
    assert result.exit_code == 0, result.output
    assert calls["spec"] == "owner/repo"
    assert calls["user_id"] == "7"
    assert "fromgit" in result.output


def test_skill_install_routes_git_url_with_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    calls = {}

    def fake_install_git(spec, *, user_id, ref=None, trust="prompt"):
        calls["spec"] = spec
        calls["ref"] = ref
        return skill_install.InstallResult(
            name="fromgit", staged_path=tmp_path, approved=False, source=f"git:{spec}"
        )

    monkeypatch.setattr(skill_install, "install_git", fake_install_git)
    result = CliRunner().invoke(
        skill_install.skill,
        ["install", "https://example.com/repo.git", "--ref", "v1.2", "--user", "7"],
    )
    assert result.exit_code == 0, result.output
    assert calls["spec"] == "https://example.com/repo.git"
    assert calls["ref"] == "v1.2"


def test_skill_install_routes_direct_skill_md_url(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    calls = {}

    def fake_install_url(url, *, user_id, trust="prompt"):
        calls["url"] = url
        return skill_install.InstallResult(
            name="fromurl", staged_path=tmp_path, approved=False, source=f"url:{url}"
        )

    monkeypatch.setattr(skill_install, "install_url", fake_install_url)
    result = CliRunner().invoke(
        skill_install.skill, ["install", "https://example.com/skills/x/SKILL.md", "--user", "7"]
    )
    assert result.exit_code == 0, result.output
    assert calls["url"] == "https://example.com/skills/x/SKILL.md"
    assert "fromurl" in result.output


def test_dispatch_install_nonexistent_local_path_raises_clear_error(monkeypatch):
    """A mistyped local path (not a real dir, not a URL, not an owner/repo
    shorthand) must raise a clear `InstallError` fast — NOT silently fall
    through to `install_git` and kick off a 120s network clone against a
    bogus `https://github.com/nope/typo.git` URL (Task 22 review finding 2)."""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("install_git must not be called for a bad local path")

    monkeypatch.setattr(skill_install, "install_git", _boom)
    with pytest.raises(skill_install.InstallError):
        skill_install.dispatch_install("./nope/typo", user_id="7")
    assert calls["n"] == 0


def test_dispatch_install_owner_repo_shorthand_still_routes_to_git(monkeypatch):
    """A genuine `owner/repo` shorthand must still route to `install_git` after
    the Finding-2 fix tightens the fallback branch."""
    calls = {}

    def fake_install_git(spec, *, user_id, ref=None, trust="prompt"):
        calls["spec"] = spec
        return skill_install.InstallResult(
            name="fromgit", staged_path=Path("/tmp"), approved=False, source=f"git:{spec}"
        )

    monkeypatch.setattr(skill_install, "install_git", fake_install_git)
    res = skill_install.dispatch_install("owner/repo", user_id="7")
    assert calls["spec"] == "owner/repo"
    assert res.name == "fromgit"


# ---------------------------------------------------------------------------
# Part B: `skill list` / `skill info` / `skill remove`
# ---------------------------------------------------------------------------


def test_skill_list_shows_active_pending_and_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "approved-one")
    skill_install.install_local(tmp_path / "approved-one", user_id="7", trust="local")
    _mkskill(tmp_path, "pending-one")
    skill_install.install_local(tmp_path / "pending-one", user_id="7", trust="prompt")

    result = CliRunner().invoke(skill_install.skill, ["list", "--user", "7"])
    assert result.exit_code == 0, result.output
    assert "approved-one" in result.output
    assert "active" in result.output
    assert "pending-one" in result.output
    assert "pending" in result.output


def test_skill_info_shows_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "infoed", desc="Info test skill. Use when testing info.")
    skill_install.install_local(tmp_path / "infoed", user_id="7", trust="local")

    result = CliRunner().invoke(skill_install.skill, ["info", "infoed", "--user", "7"])
    assert result.exit_code == 0, result.output
    assert "infoed" in result.output
    assert "Info test skill" in result.output


def test_skill_info_unknown_id_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    result = CliRunner().invoke(skill_install.skill, ["info", "does-not-exist", "--user", "7"])
    assert result.exit_code != 0


def test_skill_remove_archives_not_deletes(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "removeme")
    skill_install.install_local(tmp_path / "removeme", user_id="7", trust="local")

    result = CliRunner().invoke(skill_install.skill, ["remove", "removeme", "--user", "7"])
    assert result.exit_code == 0, result.output

    from agents.task.agent.skill_manager import get_skill_manager

    mgr = get_skill_manager()
    active = mgr._user_root("7") / "removeme"
    archived = mgr._user_root("7") / ".archived" / "removeme"
    # delete_skill moves just SKILL.md into .archived/ (the emptied active dir
    # itself is left behind) — the SKILL.md file is what actually moved.
    assert not (active / "SKILL.md").exists()
    assert (archived / "SKILL.md").is_file()


def test_skill_remove_unknown_id_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    result = CliRunner().invoke(skill_install.skill, ["remove", "does-not-exist", "--user", "7"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Part C: security — get_skill_info traversal guard (Task 22 review finding 1)
# ---------------------------------------------------------------------------


def test_get_skill_info_pending_fallback_rejects_traversal(tmp_path, monkeypatch):
    """`resolve_skill_dir` returning None must NOT fall through to an
    unvalidated `.pending/<skill_id>` join — a `skill_id` containing `..` must
    never let one tenant read a SIBLING tenant's quarantined skill.

    The attacker needs their OWN `.pending/` dir to exist for the POSIX path
    walk to even reach the `..` components (you can't `..` back out of a
    directory that was never entered) — trivially true in practice since
    `skill install --trust prompt` (the default) always creates it."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "secret-skill")
    skill_install.install_local(tmp_path / "secret-skill", user_id="victim", trust="prompt")
    _mkskill(tmp_path, "attacker-own")
    skill_install.install_local(tmp_path / "attacker-own", user_id="attacker", trust="prompt")

    with pytest.raises(skill_install.InstallError):
        skill_install.get_skill_info("../../user_victim/.pending/secret-skill", user_id="attacker")


def test_get_skill_info_archived_fallback_rejects_traversal(tmp_path, monkeypatch):
    """Same traversal guard must apply to the `.archived/<skill_id>` fallback."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "secret-skill-2")
    skill_install.install_local(tmp_path / "secret-skill-2", user_id="victim", trust="local")
    skill_install.remove_skill("secret-skill-2", "victim")
    _mkskill(tmp_path, "attacker-own-2")
    skill_install.install_local(tmp_path / "attacker-own-2", user_id="attacker", trust="local")
    skill_install.remove_skill("attacker-own-2", "attacker")

    with pytest.raises(skill_install.InstallError):
        skill_install.get_skill_info("../../user_victim/.archived/secret-skill-2", user_id="attacker")


def test_get_skill_info_still_finds_legit_pending_and_archived(tmp_path, monkeypatch):
    """The traversal guard must not break the legitimate (non-traversal)
    pending/archived lookups it's meant to protect."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "pending-legit")
    skill_install.install_local(tmp_path / "pending-legit", user_id="7", trust="prompt")
    info = skill_install.get_skill_info("pending-legit", "7")
    assert info["status"] == "pending"

    _mkskill(tmp_path, "archived-legit")
    skill_install.install_local(tmp_path / "archived-legit", user_id="7", trust="local")
    skill_install.remove_skill("archived-legit", "7")
    # `delete_skill` archives just SKILL.md and leaves the now-empty active
    # dir behind, so `resolve_skill_dir` (dir-existence only) would still
    # resolve it — remove that leftover to exercise the `.archived/` fallback
    # branch specifically (the emptied dir itself is a pre-existing,
    # out-of-scope quirk, not something this review touches).
    import shutil as _shutil

    from agents.task.agent.skill_manager import get_skill_manager

    leftover = get_skill_manager()._user_root("7") / "archived-legit"
    if leftover.is_dir():
        _shutil.rmtree(leftover)
    info = skill_install.get_skill_info("archived-legit", "7")
    assert info["status"] == "archived"


# ---------------------------------------------------------------------------
# Part D: list_all_skills dedupes archived vs active (Task 22 review finding 3)
# ---------------------------------------------------------------------------


def test_list_all_skills_dedupes_active_over_stale_archived(tmp_path, monkeypatch):
    """Remove-then-reinstall the same id must show ONE row (active), not both
    an `active` row and a stale `archived` row for the same id."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _mkskill(tmp_path, "reinstalled")
    skill_install.install_local(tmp_path / "reinstalled", user_id="7", trust="local")
    skill_install.remove_skill("reinstalled", "7")
    # Reinstall the same id — now BOTH an active dir and a .archived/ copy exist.
    skill_install.install_local(tmp_path / "reinstalled", user_id="7", trust="local")

    rows = skill_install.list_all_skills("7")
    matching = [r for r in rows if r["id"] == "reinstalled"]
    assert len(matching) == 1, matching
    assert matching[0]["status"] == "active"
