"""The deploy's subprocess import-test must cover every module the deploy
ITSELF executes after the family is stopped.

Evidence (2026-09-21 20:31Z): `import OK` passed (CLI container + telegram
surface) and the deploy then ABORTED at `running DB migrations` with
`ModuleNotFoundError: google.generativeai` raised from `core/initialization.py`
— the migration runner's import chain, which the import-test never touched. The
family was already stopped, so Rob was down ~2 min until the rollback. This
pins the import-test to the migration runner's chain so that class aborts
BEFORE the stop, with the old build still serving.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _import_test_line() -> str:
    text = (REPO / "scripts/deploy_prod.sh").read_text(encoding="utf-8")
    m = re.search(r'_import_hits="\$\(.*?print\(\'IMPORT_OK\'\).*?\)"', text, re.S)
    assert m, "deploy_prod.sh no longer has the IMPORT_OK subprocess import-test"
    return m.group(0)


def test_import_test_covers_the_migration_runner_chain():
    line = _import_test_line()
    assert "import migrations.migrate" in line
    assert "from core.initialization import initialize_core" in line


def test_import_test_still_covers_the_primary_unit_surface():
    line = _import_test_line()
    assert "build_cli_container" in line
    assert "cli.commands.telegram" in line and "surfaces.telegram" in line


def test_import_test_covers_every_surface_launch_module():
    """064: a surface the factory adds must fail the deploy gate, not the next start."""
    line = _import_test_line()
    assert "core.surfaces.catalog import SURFACES" in line
    assert "s.module + '.launch'" in line
