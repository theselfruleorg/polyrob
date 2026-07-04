"""T0.1 — schema-version honesty.

`DatabaseVersionManager.CURRENT_VERSION` must reflect the highest shipped migration
in `migrations/versions/`, not a hand-typed constant that drifts. The update flow
reports the schema axis separately from the app version, so this value has to be
truthful for `migrate.py status` and the `polyrob update` verify step to mean anything.
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VERSIONS_DIR = _REPO_ROOT / "migrations" / "versions"


def _latest_migration_version_from_files() -> str:
    versions: list[tuple[int, int, int]] = []
    for p in _VERSIONS_DIR.glob("v*.py"):
        m = re.match(r"v(\d+)_(\d+)_(\d+)_", p.name)
        if m:
            versions.append(tuple(int(x) for x in m.groups()))  # type: ignore[arg-type]
    assert versions, f"no migration files found in {_VERSIONS_DIR}"
    return ".".join(str(x) for x in max(versions))


def test_current_version_matches_latest_migration():
    from migrations.version_manager import DatabaseVersionManager

    assert DatabaseVersionManager.CURRENT_VERSION == _latest_migration_version_from_files()


def test_latest_migration_version_helper_agrees():
    """The production helper must agree with an independent scan of the files."""
    from migrations.version_manager import latest_migration_version

    assert latest_migration_version() == _latest_migration_version_from_files()
