"""Codex in-wheel review P1: the FIRST update from the four-dist layout runs the
PREVIOUS updater (already imported), which never retires the old separate pack
dists. Their `polyrob==1.1.*` pin would then fail `pip check` in verify once
core moves past 1.1. The one step of that old updater that executes code from
the TARGET tree before verify is `python -m migrations.migrate upgrade`, so
the target tree retires them there (core/packs/retire.py).
"""
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

from cli.update.detect import EDITABLE_GIT, InstallContext
from core.packs.index import RETIRED_DISTS

REPO = Path(__file__).resolve().parents[4]
OLD = "4aeb9556b"   # the last revision with the separate pack dists and its updater


def _old_module(name, rel, monkeypatch):
    try:
        src = subprocess.run(["git", "show", f"{OLD}:{rel}"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("the previous updater is not in this checkout's history")
    mod = types.ModuleType(name)
    mod.__file__ = str(REPO / rel)
    monkeypatch.setitem(sys.modules, name, mod)   # dataclasses resolve their module
    exec(compile(src, rel, "exec"), mod.__dict__)
    return mod


def _bundling_tree(root: Path) -> Path:
    (root / "requirements.lock").write_text("tweepy==4.17.0\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "polyrob"\nversion = "1.2.0"\n'
        '[project.optional-dependencies]\ntwitter = ["tweepy"]\n'
        '[project.entry-points."polyrob.packs"]\nx = "polyrob_x:pack"\n')
    return root


def test_the_previous_updater_retires_nothing_but_runs_the_target_migrate_before_verify(
        tmp_path, monkeypatch):
    import cli.update
    old_packs = _old_module("cli.update.packs", "cli/update/packs.py", monkeypatch)
    old_runners = _old_module("cli.update._old_runners", "cli/update/runners.py", monkeypatch)
    monkeypatch.setattr(cli.update, "packs", old_packs, raising=False)
    monkeypatch.setattr(old_packs, "installed_packs",
                        lambda: [old_packs.InstalledPack(id="x", dist=n, version="1.1.0")
                                 for n in sorted(RETIRED_DISTS)[-1:]])
    repo = _bundling_tree(tmp_path)
    calls = []

    def capture(cmd, cwd):
        calls.append(cmd)
        return "" if cmd[:2] == ["git", "status"] else "main"

    r = old_runners.build_runners(InstallContext(EDITABLE_GIT, repo, repo, "editable"),
                                  python="/py", run=lambda c, cwd: calls.append(c),
                                  capture=capture, extras=["twitter"])
    r.install()
    assert not any("uninstall" in c for c in calls), "the old updater never retires"
    calls.clear()
    r.migrate()
    assert calls == [["/py", "-I", "-m", "migrations.migrate", "upgrade"]]
    # ...and the engine runs migrate before verify (pip check).
    engine = (REPO / "cli/update/engine.py").read_text()
    assert engine.index('("migrate", runners.migrate)') < engine.index('("verify", runners.verify)')


def test_the_target_trees_migrate_upgrade_retires_before_migrating(monkeypatch):
    """`python -m migrations.migrate upgrade` (the old updater's migrate step)
    retires the old dists first; `status` never does."""
    import migrations.migrate as mig
    import core.packs.retire as rt
    order = []
    monkeypatch.setattr(rt, "main", lambda argv=None: order.append("retire") or 0)

    async def fake_run(command):
        order.append(command)
        return True
    monkeypatch.setattr(mig, "run_migrations", fake_run)
    assert mig.cli_main(["upgrade"]) == 0
    assert order == ["retire", "upgrade"]
    order.clear()
    assert mig.cli_main(["status"]) == 0
    assert order == ["status"]


@pytest.mark.parametrize("rc, ok, ran", [(2, False, []), (1, True, ["upgrade"])])
def test_migrate_upgrade_fails_only_on_a_double_provider(monkeypatch, caplog, rc, ok, ran):
    """The helper's result is never ignored: a pack entry point still provided
    twice breaks loading, so `upgrade` fails (the update's verify rolls back);
    any other retirement problem is a named error and the schema still migrates."""
    import logging
    import migrations.migrate as mig
    import core.packs.retire as rt
    order = []
    monkeypatch.setattr(rt, "main", lambda argv=None: rc)

    async def fake_run(command):
        order.append(command)
        return True
    monkeypatch.setattr(mig, "run_migrations", fake_run)
    with caplog.at_level(logging.ERROR, logger="migrations"):
        assert (mig.cli_main(["upgrade"]) == 0) is ok
    assert order == ran
    assert any("retired pack distributions" in r.getMessage() for r in caplog.records)
