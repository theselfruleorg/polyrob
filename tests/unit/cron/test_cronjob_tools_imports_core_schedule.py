"""U1 phase 1: the cron tool reads the ONE schedule parser from ``core.schedule``,
not through the ``cron.schedule`` re-export."""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_cronjob_tools_imports_core_schedule():
    tree = ast.parse((REPO / "tools/cronjob_tools.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "cron.schedule" not in mods
    assert "core.schedule" in mods
