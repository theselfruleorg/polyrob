"""An unreadable skill source is named; it does not crash the catalog, and it is not
"no skills" (``PermissionError`` on ``~/.agents/skills`` when HOME cannot be read)."""
import os
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager
from core.skills import scopes
from surfaces.telegram import skills_ops

pytestmark = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                reason="root reads everything; there is nothing to lock")


def _skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d {name}\n---\n# {name}\n")


@pytest.fixture(autouse=True)
def _clean():
    scopes._UNREADABLE.clear()
    scopes._UNREADABLE_LOGGED.clear()
    yield
    scopes._UNREADABLE.clear()
    scopes._UNREADABLE_LOGGED.clear()


@pytest.fixture
def locked(tmp_path):
    """A home the user cannot enter, so ``is_dir`` raises instead of answering False."""
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0)
    yield home / ".agents" / "skills"
    os.chmod(home, 0o700)


def test_a_locked_root_is_named_not_raised(locked, caplog):
    assert scopes.discover_skills(locked, "user") == []
    assert scopes.unreadable_sources() == [str(locked)]
    assert "could not read" in caplog.text and str(locked) in caplog.text


def test_a_missing_root_is_simply_empty(tmp_path):
    assert scopes.discover_skills(tmp_path / "nothing", "user") == []
    assert scopes.unreadable_sources() == []


def test_one_locked_skill_dir_keeps_its_siblings_and_is_named(tmp_path):
    _skill(tmp_path, "good")
    (tmp_path / "shut").mkdir()
    os.chmod(tmp_path / "shut", 0)
    try:
        assert {s.skill_id for s in scopes.discover_skills(tmp_path, "user")} == {"good"}
        assert scopes.unreadable_sources() == [str(tmp_path)]
    finally:
        os.chmod(tmp_path / "shut", 0o700)


def test_a_source_that_recovers_is_no_longer_named(tmp_path):
    os.chmod(tmp_path, 0)
    try:
        scopes.discover_skills(tmp_path / "skills", "user")
        assert scopes.unreadable_sources()
    finally:
        os.chmod(tmp_path, 0o700)
    scopes.discover_skills(tmp_path / "skills", "user")
    assert scopes.unreadable_sources() == []


def test_the_home_shows_as_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    scopes._UNREADABLE[str(tmp_path / ".agents" / "skills")] = "PermissionError"
    assert scopes.unreadable_sources() == ["~/.agents/skills"]


def test_the_catalog_lists_the_other_sources_and_names_the_locked_one(
        locked, tmp_path, monkeypatch):
    other = tmp_path / "other"
    _skill(other, "ecosystem-skill")
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [locked, other])
    sm = SkillManager()
    entries = sm.get_catalog_skills(user_id="u", max_skills=None)
    assert "ecosystem-skill" in {m.skill_id for m in entries}

    text = sm.format_skill_catalog(entries)
    assert f"Could not read skills from: {locked}" in text
    assert text.rstrip().endswith("</skill-catalog>")


def test_a_room_never_reads_the_owners_paths(locked, monkeypatch):
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [locked])
    sm = SkillManager()
    entries = sm.get_catalog_skills(user_id="u", max_skills=None, third_party=True)
    assert entries, "the fixture needs shipped skills"
    assert str(locked) not in sm.format_progressive([], entries, set(), third_party=True)


def test_slash_skills_names_the_source(locked, monkeypatch):
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [locked])
    from agents.task.agent import skill_manager
    monkeypatch.setattr(skill_manager, "get_skill_manager", lambda: SkillManager())
    reply = skills_ops.skills_reply("u", [])
    assert "skill(s) I can load" in reply
    assert f"I could not read skills from {locked}" in reply
    assert "not 'no skills'" in reply


def test_slash_skills_with_every_source_readable_says_nothing_extra(monkeypatch):
    from agents.task.agent import skill_manager
    monkeypatch.setattr(skill_manager, "get_skill_manager", lambda: SkillManager())
    assert "could not read" not in skills_ops.skills_reply("u", [])
