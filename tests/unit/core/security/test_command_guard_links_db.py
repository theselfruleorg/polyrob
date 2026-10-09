"""A relative delete through a symlink, and a sqlite3 write to the agent's own
state databases by file name (verifier round 3, 2026-10-08).

Both used to answer ``ok``: ``relative_delete_refusal`` normalised the path
without resolving links (``ln -s .. up`` then ``rm -rf up/data``), and the
sqlite rule needed a ``.polyrob``/``/polyrob`` component in the path
(``sqlite3 ../cron.db "update …"``). The natural flows stay ok.
"""
import os

import pytest

from core.security.command_guard import DANGEROUS, OK, classify, relative_delete_refusal


# ── symlinks ────────────────────────────────────────────────────────────────

@pytest.fixture
def tree():
    # Under /tmp, not pytest's tmp_path: on macOS that is /private/var/…, and
    # `/private` is itself a protected system tree for a relative delete.
    import shutil
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="guardlinks-", dir="/tmp"))
    yield from _tree(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def _tree(tmp):
    root = tmp / "srv"
    data = root / "data"
    (data / "wallet").mkdir(parents=True)
    (data / "cron.db").write_text("")
    ws = data / "auto" / "u" / "sessions" / "s" / "workspace"
    ws.mkdir(parents=True)
    proj = root / "project"
    (proj / "build").mkdir(parents=True)
    (proj / "node_modules").mkdir()
    (proj / "dist").mkdir()
    (proj / ".pytest_cache").mkdir()
    yield {"root": root, "data": data, "proj": proj, "ws": ws}


def _refuse(cmd, cwd, data):
    return relative_delete_refusal(cmd, str(cwd), home=None, data_home=str(data),
                                   resolve_links=True)


def test_delete_through_an_existing_symlink_is_refused(tree):
    os.symlink("..", tree["proj"] / "up")
    why = _refuse("rm -rf up/data", tree["proj"], tree["data"])
    assert why and "symlink" in why


def test_delete_through_a_symlink_made_in_the_same_line_is_refused(tree):
    assert _refuse("ln -s .. up && rm -rf up/data", tree["proj"], tree["data"])
    assert _refuse("ln -sf ../data d; rm -rf d/*", tree["proj"], tree["data"])


def test_cd_into_a_symlink_then_delete_is_refused(tree):
    os.symlink(str(tree["data"]), tree["proj"] / "p")
    assert _refuse("cd p && rm -rf wallet", tree["proj"], tree["data"])


def test_the_data_home_and_its_parents_are_never_a_relative_target(tree):
    assert _refuse("rm -rf data", tree["root"], tree["data"])
    assert _refuse("rm -rf wallet", tree["data"], tree["data"])
    assert _refuse("rm -rf cron.db", tree["data"], tree["data"])


def test_removing_a_link_itself_stays_ok(tree):
    os.symlink("..", tree["proj"] / "up")
    assert _refuse("rm -rf up", tree["proj"], tree["data"]) is None


@pytest.mark.parametrize("cmd", [
    "rm -rf build/ dist/ node_modules/ .pytest_cache",
    "rm -rf build",
    "rm -rf dist/*",
    "cd build && rm -rf x",
])
def test_natural_cleanup_stays_ok(tree, cmd):
    (tree["proj"] / "build" / "x").mkdir()
    assert _refuse(cmd, tree["proj"], tree["data"]) is None


def test_natural_cleanup_inside_a_session_workspace_stays_ok(tree):
    (tree["ws"] / "build").mkdir()
    assert _refuse("rm -rf build", tree["ws"], tree["data"]) is None


@pytest.mark.parametrize("cmd", [
    "echo x > ~/.polyrob/data/wallet/keystore.json",
    "rm -rf ~/.polyrob/data/wallet",
    "cp evil.jsonl /home/a/.polyrob/data/wallet/audit.jsonl",
])
def test_local_data_home_wallet_is_protected(cmd):
    assert classify(cmd).level != OK


# ── sqlite3 on the agent's own databases ─────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "sqlite3 ../cron.db \"update cron_jobs set payload='{}'\"",
    "cd .. && sqlite3 data/cron.db \"update cron_jobs set payload='{}'\"",
    "D=..; sqlite3 $D/data/goals.db 'update goals set payload=1'",
    "sqlite3 pairing.db \"insert into codes values (1)\"",
    "sqlite3 cards.db 'delete from cards'",
    "sqlite3 verdicts.db",
    "sqlite3 -cmd '.shell id' goals.db 'select 1'",
    "sqlite3 goals.db '.read evil.sql'",
])
def test_writes_to_state_dbs_need_the_owner(cmd):
    assert classify(cmd).level == DANGEROUS, classify(cmd)


@pytest.mark.parametrize("cmd", [
    "sqlite3 ../cron.db 'select id, task from cron_jobs'",
    "sqlite3 data/goals.db .schema",
    "sqlite3 app.db \"insert into todo values ('x')\"",
    "sqlite3 build/test.db 'create table t (a)'",
])
def test_reads_and_the_agents_own_app_dbs_stay_ok(cmd):
    assert classify(cmd).level == OK, classify(cmd)


# ── the server layout: POLYROB_DATA_DIR=/var/lib/polyrob ──────────────────────
# The data home holds the agent's OWN work folders: the shared project folder
# (/var/lib/polyrob/project) and the session workspaces under data/auto/…. A
# relative delete below them is plain work; the data home itself, its state and
# the work folders' own roots stay refused.

@pytest.mark.parametrize("project_env", ["", "/var/lib/polyrob/project"])
@pytest.mark.parametrize("cwd", [
    "/var/lib/polyrob/project",
    "/var/lib/polyrob/project/app",
    "/var/lib/polyrob/data/auto/u1/sessions/s1/workspace",
])
def test_server_layout_own_work_folders_allow_a_relative_delete(monkeypatch, cwd, project_env):
    monkeypatch.setenv("POLYROB_PROJECT_DIR", project_env)
    assert relative_delete_refusal("rm -rf build", cwd, data_home="/var/lib/polyrob") is None


@pytest.mark.parametrize("cmd,cwd", [
    ("rm -rf data", "/var/lib/polyrob"),
    ("rm -rf project", "/var/lib/polyrob"),
    ("rm -rf wallet", "/var/lib/polyrob/data"),
    ("rm -rf s1", "/var/lib/polyrob/data/auto/u1/sessions"),
])
def test_server_layout_state_stays_refused(monkeypatch, cmd, cwd):
    monkeypatch.setenv("POLYROB_PROJECT_DIR", "/var/lib/polyrob/project")
    assert relative_delete_refusal(cmd, cwd, data_home="/var/lib/polyrob")
