"""migrations/ must ship in the installed wheel.

Fresh-install finding (2026-07-19): `polyrob doctor` printed
"db schema: unknown (could not resolve code schema version)" under a pip install.
Root cause: `migrations*` was missing from `[tool.setuptools.packages.find].include`
in pyproject.toml, so a built wheel shipped ZERO files under `migrations/` (verified
by actually building the wheel and listing its contents) — `from migrations.
version_manager import latest_migration_version` then raised ModuleNotFoundError,
which `cli/commands/doctor.py::schema_status_line` catches and reports as "unknown".
This is read-only version *resolution*; auto-applying migrations on a pip install
stays a separate, deliberately-unaddressed item (the updater prints the manual
command by design) — untouched by this fix.
"""
from pathlib import Path

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

_REPO = Path(__file__).resolve().parents[2]


def _pyproject():
    with open(_REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_packages_find_includes_migrations():
    cfg = _pyproject()
    include = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(p.startswith("migrations") for p in include), include


def test_latest_migration_version_resolves_from_the_real_versions_dir():
    """Sanity check the read-only resolver itself still works in-tree (the
    packaging fix only matters once installed — this guards the function apart
    from packaging)."""
    from migrations.version_manager import latest_migration_version
    version = latest_migration_version()
    assert version != "unknown"
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), version
