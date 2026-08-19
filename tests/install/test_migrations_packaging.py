"""Migrations must ship in the wheel and run on the CLI path (proposal 027 WP2).

The published 0.10.0 wheel omitted migrations/ for two independent reasons:
pyproject's packages.find include list lacked "migrations*", and the directory
had no __init__.py so find_packages skipped it. On top of that,
build_cli_container never called run_boot_migrations, so even git installs
never migrated on `polyrob run`.
"""

import inspect
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_includes_migrations_package():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"migrations*"' in pyproject, (
        "packages.find.include must list migrations* or the wheel omits the "
        "package polyrob update tells users to run"
    )


def test_migrations_is_a_regular_package():
    assert (REPO_ROOT / "migrations" / "__init__.py").exists()
    assert (REPO_ROOT / "migrations" / "versions" / "__init__.py").exists()


def test_python_dash_m_migrate_status_works(tmp_path):
    """The exact command the docs and `polyrob update` prescribe."""
    env = dict(os.environ)
    env["POLYROB_DATA_DIR"] = str(tmp_path / "data")
    env["DB_PATH"] = str(tmp_path / "data" / "bot.db")
    proc = subprocess.run(
        [sys.executable, "-m", "migrations.migrate", "status"],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_build_cli_container_runs_boot_migrations():
    import core.bootstrap as bootstrap

    src = inspect.getsource(bootstrap.build_cli_container)
    assert "run_boot_migrations" in src, (
        "the CLI container must migrate bot.db at boot — otherwise `polyrob run` "
        "can hit 'no such column' on any upgraded install"
    )
