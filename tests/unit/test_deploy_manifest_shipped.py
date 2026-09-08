"""The stream manifest must be inside a directory the deploy actually syncs.

A manifest that never reaches /opt/polyrob is a stream list that silently does
nothing — the exact failure the 2026-08-25 skill-drift incident already cost us.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY = ROOT / "scripts" / "deploy_prod.sh"

# Both assertions are about the OPERATOR deploy path: scripts/ is deploy tooling
# and data/streams/ is operator instance data, and neither ships in the public
# framework export. On the private tree, where both exist, these always run.
import pytest

from agents.task.goals import streams as _S

pytestmark = pytest.mark.skipif(
    not (DEPLOY.is_file() and Path(_S.default_manifest_path()).is_file()),
    reason="deploy script + stream manifest are operator-owned, absent from the public tree",
)


def _dirs(var: str) -> set:
    for line in DEPLOY.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{var}="):
            return set(line.split("=", 1)[1].strip().strip('"').split())
    raise AssertionError(f"{var} not found in {DEPLOY}")


def test_manifest_directory_is_deployed():
    synced = _dirs("CODE_DIRS") | _dirs("BUNDLED_DIRS")
    assert "data/streams" in synced or "config" in synced, (
        "data/streams is not synced by scripts/deploy_prod.sh — the stream "
        "manifest would never reach the box")


def test_manifest_file_exists_where_the_loader_looks():
    from agents.task.goals import streams as S
    assert Path(S.default_manifest_path()).is_file()
