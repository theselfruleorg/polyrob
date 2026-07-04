"""A4: docker-compose's env_file must reference a git-TRACKED file.

`docker compose up` fails immediately if env_file points at a path that isn't present
in a fresh clone. The compose file used to reference config/.env.development, which is
gitignored — so a self-hoster's first `docker compose up` broke before anything ran.
This guards that every env_file entry resolves to a tracked, on-disk file.
"""
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"


def _env_file_paths():
    text = COMPOSE.read_text()
    # matches: env_file: [config/.env.example]  and  env_file: ["a", "b"]
    paths = []
    for m in re.finditer(r"env_file:\s*\[([^\]]*)\]", text):
        for raw in m.group(1).split(","):
            p = raw.strip().strip("\"'")
            if p:
                paths.append(p)
    return paths


def _tracked(path: str) -> bool:
    r = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    return r.returncode == 0


def test_compose_env_files_are_tracked_and_present():
    paths = _env_file_paths()
    assert paths, "expected at least one env_file entry in docker-compose.yml"
    for p in paths:
        assert (REPO_ROOT / p).is_file(), f"env_file {p!r} missing on disk"
        assert _tracked(p), (
            f"docker-compose env_file {p!r} is not git-tracked — a fresh "
            f"`docker compose up` would fail. Point it at a committed template.")
