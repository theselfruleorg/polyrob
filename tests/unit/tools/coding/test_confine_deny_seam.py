"""H11/H12 (security analysis 2026-09-23): coding._confine uses the shared seam."""
import pytest


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "far-home"))


def _tool():
    from tools.coding.tool import CodingTool
    return object.__new__(CodingTool)


@pytest.mark.parametrize("rel", [".git/config", ".agents/skills/x/SKILL.md",
                                 ".claude/skills/y/tool.sh", "AGENTS.md", "CLAUDE.md"])
def test_write_refused(tmp_path, rel):
    from tools.coding.tool import CodingError
    with pytest.raises(CodingError):
        _tool()._confine(rel, str(tmp_path))


def test_read_of_context_file_and_skill_allowed_git_refused(tmp_path):
    from tools.coding.tool import CodingError
    t = _tool()
    assert t._confine("AGENTS.md", str(tmp_path), write=False)
    assert t._confine(".claude/skills/y/SKILL.md", str(tmp_path), write=False)
    with pytest.raises(CodingError):
        t._confine(".git/config", str(tmp_path), write=False)


def test_local_data_home_refused(tmp_path, monkeypatch):
    from tools.coding.tool import CodingError
    (tmp_path / ".polyrob").mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / ".polyrob"))
    for write in (True, False):
        with pytest.raises(CodingError, match="data home"):
            _tool()._confine(".polyrob/goals.db", str(tmp_path), write=write)
    assert _tool()._confine("src/app.py", str(tmp_path))
