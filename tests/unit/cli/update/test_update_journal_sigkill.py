"""098 CL-09: a SIGKILL in the middle of `update --apply` is rolled back by the
next run's recovery. A real subprocess runs ``apply_update`` with the phase
journal against a TEMP git install tree and a TEMP data home; its install step
commits new code, writes the DB, then sleeps — and is SIGKILLed there (no
``except``/``finally`` runs). The recovery (``recover_open_update`` ->
``rollback_for_boot``) must return the checkout to the old commit and restore
the pre-update snapshot. Nothing here touches the real install: the install /
update-context resolvers are pointed at the temp tree, and only ``git`` runs for
real (pip is recorded, never run).
"""
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# ~1 s; tests/conftest.py marks a subprocess-spawning module slow by rule.
pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals")

ROOT = Path(__file__).resolve().parents[4]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]

CHILD = textwrap.dedent('''
    import sqlite3, subprocess, sys, time
    from pathlib import Path
    repo, home, snaps, ready = map(Path, sys.argv[1:5])
    from cli.update.context import UpdateContext
    from cli.update.detect import GIT, InstallContext
    from cli.update.engine import UpdateRunners, apply_update
    from cli.update.journal import shield_signals, update_journal
    from cli.update.runners import build_runners

    def no_run(cmd, cwd):
        raise RuntimeError(f"the child never runs {cmd}")

    real = build_runners(InstallContext(GIT, repo, repo), run=no_run, extras=[], packs=[])
    db = home / "bot.db"
    git = %(git)r

    def install():
        (repo / "code.txt").write_text("v2\\n")
        subprocess.run(git + ["commit", "-qam", "v2"], cwd=repo, check=True)
        c = sqlite3.connect(str(db)); c.execute("INSERT INTO t VALUES (2)"); c.commit(); c.close()
        ready.write_text("1")
        time.sleep(120)

    runners = UpdateRunners(install=install, migrate=lambda: None, verify=lambda: None,
                            rollback_code=lambda: None, prepare=real.prepare)
    ctx = UpdateContext(data_home=home, snapshots_root=snaps, db_paths=[db])
    with shield_signals():
        apply_update(ctx=ctx, runners=runners, from_version="1", to_version="2",
                     journal=update_journal(snaps))
''') % {"git": GIT}


def _git(repo, *args):
    return subprocess.run(GIT + list(args), cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _rows(db):
    c = sqlite3.connect(str(db))
    try:
        return [r[0] for r in c.execute("SELECT id FROM t ORDER BY id")]
    finally:
        c.close()


def test_sigkill_mid_apply_is_rolled_back(tmp_path, monkeypatch):
    repo, home, snaps = tmp_path / "install", tmp_path / "home", tmp_path / "snaps"
    repo.mkdir()
    home.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "code.txt").write_text("v1\n")
    _git(repo, "add", "code.txt")
    _git(repo, "commit", "-qm", "v1")
    old_sha = _git(repo, "rev-parse", "HEAD")
    db = home / "bot.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE t (id INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()

    script = tmp_path / "child.py"
    script.write_text(CHILD)
    ready = tmp_path / "ready"
    env = dict(os.environ, PYTHONPATH=str(ROOT), POLYROB_DATA_DIR=str(home))
    child = subprocess.Popen([sys.executable, str(script), str(repo), str(home), str(snaps),
                              str(ready)], cwd=str(tmp_path), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert child.poll() is None, child.communicate()[1].decode()[-2000:]
            assert time.monotonic() < deadline, "the child never reached the install step"
            time.sleep(0.05)
        child.send_signal(signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    # The kill left new code and a written DB, and an open journal naming the way back.
    assert _git(repo, "rev-parse", "HEAD") != old_sha and _rows(db) == [1, 2]
    from cli.update.journal import recover_open_update, update_journal
    entry = update_journal(snaps).pending()
    assert entry["phase"] == "install" and entry["info"]["old_sha"] == old_sha

    # Point the real rollback at the TEMP tree only; git runs, pip is recorded.
    import cli.commands.update as upd
    from cli.update.context import UpdateContext
    from cli.update.detect import GIT as GIT_METHOD, InstallContext
    from cli.update.runners import build_runners as real_build
    skipped = []

    def git_only_run(cmd, cwd):
        assert Path(cwd) == repo, f"a rollback step ran outside the temp tree: {cwd}"
        if cmd[0] == "git":
            subprocess.run(GIT + list(cmd[1:]), cwd=cwd, check=True, capture_output=True)
        else:
            skipped.append(cmd)

    def git_only_capture(cmd, cwd):
        if cmd[0] == "git":
            return _git(cwd, *cmd[1:])
        return ""

    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setattr(upd, "detect_install", lambda: InstallContext(GIT_METHOD, repo, repo))
    monkeypatch.setattr(upd, "resolve_update_context",
                        lambda *a, **k: UpdateContext(data_home=home, snapshots_root=snaps,
                                                      db_paths=[db]))
    monkeypatch.setattr(upd, "build_runners", lambda ictx, **kw: real_build(
        ictx, run=git_only_run, capture=git_only_capture, extras=[], packs=[], **kw))
    assert upd.detect_install().repo_root == repo  # never the real install

    out = recover_open_update(snaps, echo=lambda *_: None)

    assert out == "rolled_back"
    assert _git(repo, "rev-parse", "HEAD") == old_sha
    assert (repo / "code.txt").read_text() == "v1\n"
    assert _rows(db) == [1], "the pre-update snapshot was restored"
    assert update_journal(snaps).pending() is None
    assert update_journal(snaps).read()["phase"] == "rolled_back"
