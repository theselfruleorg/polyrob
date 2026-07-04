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
    from tools.mcp.security import _key_file_path

    monkeypatch.chdir(tmp_path)
    p = _key_file_path()
    assert p.is_absolute()
    assert not str(p).startswith(str(tmp_path))
    assert p == REPO_ROOT / "data" / ".mcp_encryption_key"


def test_prompts_default_dir_anchored():
    import agents.prompt as ap

    assert ap.DEFAULT_PROMPTS_DIR.is_absolute()
    assert ap.DEFAULT_PROMPTS_DIR == REPO_ROOT / "data" / "prompts"


def test_skills_base_dir_anchored():
    from api.skill_endpoints import get_skills_base_dir

    assert get_skills_base_dir().is_absolute()
    assert get_skills_base_dir() == REPO_ROOT / "data" / "prompts" / "skills"


def test_auto_db_path_anchored():
    from modules.database.connection import _auto_db_path

    assert Path(_auto_db_path()).is_absolute()
