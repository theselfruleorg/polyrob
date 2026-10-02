"""Phase 5 (path-concerns upgrade): module-level data/ paths must be CWD-invariant.

A2/A4/A5: several modules used bare relative Path("data/...") constants that
resolve against the process CWD — fine when launched from the repo root, but the
MCP Fernet key in particular regenerates (orphaning encrypted creds) if CWD varies.
Anchor them to the install/repo root.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_mcp_key_path_is_absolute_and_cwd_independent(tmp_path, monkeypatch):
    """The dev Fernet key lives in the DATA HOME (never the install tree, which is
    site-packages for a pip install); with the data home pinned it is CWD-invariant."""
    from tools.mcp.security import _key_file_path

    home = tmp_path / "home"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.chdir(tmp_path)
    p = _key_file_path()
    assert p.is_absolute()
    assert p == home.resolve() / ".mcp_encryption_key"
    assert not str(p).startswith(str(REPO_ROOT / "core"))


def test_prompts_default_dir_anchored():
    import agents.prompt as ap

    assert ap.DEFAULT_PROMPTS_DIR.is_absolute()
    assert ap.DEFAULT_PROMPTS_DIR == REPO_ROOT / "data" / "prompts"


def test_skills_base_dir_anchored():
    from api.skill_endpoints import get_skills_base_dir

    assert get_skills_base_dir().is_absolute()
    assert get_skills_base_dir() == REPO_ROOT / "data" / "prompts" / "skills"
