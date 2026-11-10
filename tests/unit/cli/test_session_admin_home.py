"""113 W2 item 5 (T-04, T-05): every `polyrob session` verb reads the admin home's
tree; `session list` shows a title and the chat each session is in, says how many
finished ones it hid, and reports an unreadable store instead of an empty one."""
import json

import pytest
from click.testing import CliRunner


def _runner():
    """Split-stream runner across click versions: 8.1 needs mix_stderr=False
    for a separate .stderr; 8.2+ removed the kwarg and always splits."""
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2
        return CliRunner()


@pytest.fixture
def home(monkeypatch, tmp_path):
    from cli.commands import session as sess
    monkeypatch.setattr(sess, "_admin_home", lambda write=False: tmp_path)
    monkeypatch.setattr(sess, "ensure_env_loaded", lambda: None)
    return tmp_path


def _seed(home, sid, status, task=None, user="tenant-a", updated="2026-01-01T00:00:00"):
    d = home / "sessions" / user / sid
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps(
        {"id": sid, "user_id": user, "status": status, "task": task,
         "created_at": updated, "updated_at": updated}))
    return d


def _bind(home, sid, *, slug="main", label="", user="tenant-a"):
    from core.surfaces.session_chat_registry import SessionChatRegistry, chat_session_key
    reg = SessionChatRegistry(str(home / "surfaces.db"))
    base = f"agent:main:repl:dm:local:{user}"
    key = base if slug == "main" else chat_session_key(base, slug)
    reg.bind(key, sid, user, "repl", "local")
    if label:
        reg.set_label(key, label)


def _list(*args):
    from cli.commands.session import session
    return _runner().invoke(session, ["list", *args])


def test_data_root_is_the_admin_home_tree(home, monkeypatch):
    from cli.commands import session as sess
    monkeypatch.delenv("DATA_ROOT", raising=False)
    assert sess._data_root() == home / "sessions"
    (home / "sessions").mkdir()
    monkeypatch.setenv("DATA_ROOT", str(home / "elsewhere"))
    assert sess._data_root() == home / "sessions"  # the admin tree wins when it exists


def test_a_refused_admin_home_is_an_error_not_another_tree(monkeypatch):
    import click
    from cli.commands import session as sess

    def _refuse(write=False):
        raise click.ClickException("ambiguous data home")

    monkeypatch.setattr(sess, "_admin_home", _refuse)
    with pytest.raises(click.ClickException):
        sess._data_root()


def test_list_shows_title_and_chat_name(home):
    _seed(home, "sess-aaa", "running", task="write the report\nsecond line")
    _seed(home, "sess-bbb", "created", updated="2026-02-01T00:00:00")
    _bind(home, "sess-bbb", slug="c2", label="Work")
    res = _list()
    assert res.exit_code == 0, res.output + res.stderr
    lines = res.output.splitlines()
    assert "Chat" in lines[0] and "Title" in lines[0]
    assert lines[2].startswith("sess-bbb")  # newest first
    assert "Work" in lines[2] and "Work chat in the terminal" in lines[2]
    assert "write the report" in lines[3] and "second line" not in res.output


def test_list_hides_finished_and_says_so(home):
    _seed(home, "sess-done", "completed", task="old")
    res = _list()
    assert res.exit_code == 0
    assert "No live sessions. 1 finished" in res.output and "--all" in res.output
    res = _list("--all")
    assert "sess-done" in res.output and "old" in res.output


def test_list_json_carries_title_and_chat(home):
    _seed(home, "sess-aaa", "running", task="t1")
    _bind(home, "sess-aaa")
    rows = json.loads(_list("--json").output)
    assert rows[0]["title"] == "t1"
    assert rows[0]["chat"]["name"] == "main" and rows[0]["chat"]["seat"] == "the terminal"


def test_list_reports_an_unreadable_chat_store(home):
    _seed(home, "sess-aaa", "running", task="t1")
    (home / "surfaces.db").write_text("not a database")
    res = _list("--json")
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert rows[0]["chat"] is None and rows[0]["chat_error"]
    assert "could not read the chat store" in res.stderr


def test_list_names_an_unreadable_metadata_file(home):
    _seed(home, "sess-aaa", "running", task="t1")
    bad = home / "sessions" / "tenant-a" / "sess-bad"
    bad.mkdir()
    (bad / "metadata.json").write_text("{torn")
    res = _list()
    assert "sess-aaa" in res.output
    assert "could not read" in res.stderr and "sess-bad" in res.stderr


def test_list_never_rewrites_a_live_run(home):
    """A SessionManager load marks `running` as suspended on disk; the list must not."""
    d = _seed(home, "sess-live", "running", task="t")
    _list("--all")
    assert json.loads((d / "metadata.json").read_text())["status"] == "running"


def test_show_names_the_chat(home):
    _seed(home, "sess-aaa", "running", task="t1")
    _bind(home, "sess-aaa", slug="c2", label="Work")
    from cli.commands.session import session
    res = CliRunner().invoke(session, ["show", "sess-aaa"])
    assert res.exit_code == 0, res.output
    assert "chat: Work (the terminal)" in res.output


def test_control_needs_the_exact_id_in_the_admin_home(home, monkeypatch):
    from unittest.mock import AsyncMock
    d = _seed(home, "sess-aaa111", "running", task="t1")
    control = AsyncMock()
    monkeypatch.setattr("cli.commands._session_control.control_live_session", control)
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid: "tenant-a")
    from cli.commands.session import session
    CliRunner().invoke(session, ["pause", "sess-aaa111"])
    info, directory, user, sid, cmd = control.await_args.args
    assert info["id"] == "sess-aaa111" and directory == d and user == "tenant-a"
    assert cmd == "pause"
    control.reset_mock()
    CliRunner().invoke(session, ["cancel", "sess-aaa"])  # a prefix is not enough to stop
    assert control.await_args.args[:2] == (None, None)
