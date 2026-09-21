"""WS-C5 CLI polish batch (proposal 030): D7/D8/D9/D10/D13.

- D9: the `session attach` stub is DELETED (`run --resume <id>` is the real
  path; the session group epilog points there).
- D10: `knowledge` is a hidden deprecated alias — `kb export` is the front
  door; both run ONE implementation (`cli.commands.knowledge.run_export`).
- D13: every `--yes` confirmation flag also accepts `-y`.
- D8: `--json` on the listed read verbs (owner pending/asks/invoices/
  correspondents/allowlist, cron list, kb list/search, skills list,
  approvals list, profile list, surface list), following the goals.py idiom:
  plain json.dumps of the structured data, text output unchanged otherwise.
- D7: `owner`/`profile`/`session` group help is sectioned (GroupedGroup).
"""
import json

import click
import pytest
from click.testing import CliRunner


def _runner():
    """Split-stream runner across click versions: 8.1 needs mix_stderr=False
    for a separate .stderr; 8.2+ removed the kwarg and always splits."""
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2
        return CliRunner()


def _run(cmd, args, **kwargs):
    return _runner().invoke(cmd, args, **kwargs)


# ---------------------------------------------------------------------------
# D9 — `session attach` deleted; help points at run --resume
# ---------------------------------------------------------------------------

def test_session_attach_is_deleted():
    from cli.commands.session import session
    assert "attach" not in session.commands
    res = _run(session, ["attach", "abc123"])
    assert res.exit_code != 0  # click's unknown-command error is the answer


def test_session_help_epilog_points_at_run_resume():
    from cli.commands.session import session
    res = _run(session, ["--help"])
    assert res.exit_code == 0
    assert "run --resume" in res.output


# ---------------------------------------------------------------------------
# D7 — sectioned subcommand help
# ---------------------------------------------------------------------------

def test_owner_help_is_sectioned():
    from cli.commands.owner import owner
    res = _run(owner, ["--help"])
    assert res.exit_code == 0
    for title in ("Access & pairing", "Pending & asks", "Money", "Control"):
        assert title in res.output, f"missing section {title!r}"
    assert res.output.index("Access & pairing") < res.output.index("Control")
    # no stragglers: every owner verb is in a named section
    assert "Other" not in res.output


def test_profile_help_is_sectioned():
    from cli.commands.profile import profile
    res = _run(profile, ["--help"])
    assert res.exit_code == 0
    assert "Lifecycle" in res.output
    assert "Distribution" in res.output


def test_session_help_is_sectioned():
    from cli.commands.session import session
    res = _run(session, ["--help"])
    assert res.exit_code == 0
    assert "Inspect" in res.output
    assert "Control" in res.output


def test_grouped_group_lands_unlisted_commands_in_other():
    """A new subcommand missing from every section must never vanish from --help."""
    from cli.commands._grouped import GroupedGroup

    @click.group(cls=GroupedGroup, help_sections=[("Known", ["alpha"])])
    def g():
        pass

    @g.command("alpha")
    def alpha():
        pass

    @g.command("beta")
    def beta():
        pass

    res = _run(g, ["--help"])
    assert res.exit_code == 0
    assert "Known" in res.output
    assert "Other" in res.output
    assert "beta" in res.output


# ---------------------------------------------------------------------------
# D10 — `knowledge` hidden deprecated alias; `kb export` is the front door
# ---------------------------------------------------------------------------

def test_knowledge_group_is_hidden():
    from cli.commands.knowledge import knowledge
    assert knowledge.hidden is True


def test_knowledge_export_prints_deprecation_then_delegates(monkeypatch):
    import cli.commands.knowledge as kmod
    calls = []
    monkeypatch.setattr(
        kmod, "run_export",
        lambda out_dir, since, user: calls.append((out_dir, since, user)))
    res = _run(kmod.knowledge, ["export", "--out", "vault", "--user", "u1"])
    assert res.exit_code == 0, res.output
    assert calls == [("vault", None, "u1")]
    assert "deprecated" in res.stderr
    assert "kb export" in res.stderr


def test_kb_export_runs_the_same_impl_without_deprecation(monkeypatch):
    import cli.commands.knowledge as kmod
    from cli.commands.kb import kb
    calls = []
    monkeypatch.setattr(
        kmod, "run_export",
        lambda out_dir, since, user: calls.append((out_dir, since, user)))
    res = _run(kb, ["export", "--out", "vault"])
    assert res.exit_code == 0, res.output
    assert calls == [("vault", None, "local")]
    assert "deprecated" not in (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# D13 — every --yes flag accepts -y
# ---------------------------------------------------------------------------

def _yes_opts(cmd):
    param = next(p for p in cmd.params if p.name in ("yes", "assume_yes"))
    return list(param.opts) + list(param.secondary_opts)


def test_dash_y_shorthand_everywhere_yes_exists():
    from cli.commands.auth import auth
    from cli.commands.profile import profile
    from cli.commands.update import update_cmd
    from cli.commands.wallet import wallet_cmd
    for cmd in (
        auth.commands["remove"],
        profile.commands["delete"],
        wallet_cmd.commands["set-cap"],
        wallet_cmd.commands["init"],
        update_cmd,  # already had -y; pinned so it stays
    ):
        assert "-y" in _yes_opts(cmd), f"{cmd.name} lacks -y"


# ---------------------------------------------------------------------------
# D8 — --json coverage (text paths are covered by the existing per-command
# tests; these assert the JSON contract: parseable stdout, structured data)
# ---------------------------------------------------------------------------

@pytest.fixture
def owner_env(monkeypatch, tmp_path):
    from core.instance import DEFAULT_INSTANCE_ID
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    return tmp_path


def test_owner_pending_json(owner_env):
    from cli.commands.owner import owner
    from core.self_context_writer import PROVENANCE_AGENT, SelfContextWriter
    res = _run(owner, ["pending", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []

    SelfContextWriter(owner_env).propose(
        "Learned: surface blockers to the owner proactively.",
        user_id="alice", created_by=PROVENANCE_AGENT, pending=True)
    res = _run(owner, ["pending", "--json"])
    items = json.loads(res.stdout)
    assert len(items) == 1
    assert items[0]["kind"] == "self_context"


def test_owner_asks_json(owner_env):
    from agents.task.goals.board import GoalBoard
    from cli.commands.owner import owner
    res = _run(owner, ["asks", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []

    board = GoalBoard(str(owner_env / "goals.db"))
    board.create_ask(user_id="alice", what="Need an API key for X",
                     why="blocked on credentials", force=True)
    res = _run(owner, ["asks", "--json"])
    rows = json.loads(res.stdout)
    assert len(rows) == 1
    assert "Need an API key" in rows[0]["title"]


def test_owner_correspondents_json(owner_env):
    from cli.commands.owner import owner
    res = _run(owner, ["correspondents", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []


def test_owner_allowlist_json(owner_env):
    from cli.commands.owner import owner
    _run(owner, ["allow", "telegram", "12345", "--user", "u1"])
    res = _run(owner, ["allowlist", "--user", "u1", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert len(rows) == 1
    assert rows[0]["surface"] == "telegram"
    assert rows[0]["target"] == "12345"


def test_owner_invoices_json_error_is_json(owner_env):
    """No bot.db on a fresh dir — with --json even the failure is parseable."""
    from cli.commands.owner import owner
    res = _run(owner, ["invoices", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert "error" in payload


def test_cron_list_json(tmp_path):
    from cli.commands.cron import cron
    env = {"POLYROB_DATA_DIR": str(tmp_path)}
    r = _runner().invoke(cron, ["schedule", "check the feeds", "30m",
                                "--user", "u1"], env=env)
    assert r.exit_code == 0, r.output
    res = _runner().invoke(cron, ["list", "--user", "u1", "--json"], env=env)
    assert res.exit_code == 0, res.output
    jobs = json.loads(res.stdout)
    assert len(jobs) == 1
    assert jobs[0]["task"] == "check the feeds"
    assert jobs[0]["schedule_spec"] == "30m"


def test_surface_list_json(monkeypatch, tmp_path):
    from cli.commands.surface import surface
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _run(surface, ["pause", "telegram"])
    res = _run(surface, ["list", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert rows == [{"surface_id": "telegram", "paused": True}]


def test_approvals_list_json(tmp_path):
    from cli.commands.approvals import approvals
    res = _run(approvals, ["list", "--home", str(tmp_path), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    # C23: the payload now names the TENANT it resolved, because the verb used
    # to default to the literal "local" while the gate is enforced under the
    # bound owner's tenant.
    assert set(payload) == {"gates", "provider", "user_id"}
    assert isinstance(payload["gates"], dict)


def test_profile_list_json(monkeypatch, tmp_path):
    from cli.commands.profile import profile
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.setenv("POLYROB_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    res = _run(profile, ["list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == []

    r = _run(profile, ["create", "scout", "--no-alias",
                       "--description", "test bot"])
    assert r.exit_code == 0, r.output
    res = _run(profile, ["list", "--json"])
    rows = json.loads(res.stdout)
    assert len(rows) == 1
    assert rows[0]["name"] == "scout"
    assert rows[0]["description"] == "test bot"
    assert rows[0]["active"] is False


def test_skills_list_json(monkeypatch):
    from cli.commands import skills as smod

    class _Mgr:
        skill_rules = {"beta": {}, "alpha": {}}

    monkeypatch.setattr(smod, "get_skill_manager", lambda: _Mgr())
    res = _run(smod.skills, ["list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == ["alpha", "beta"]


def _async_return(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro


@pytest.fixture
def kb_stubbed(monkeypatch):
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr("cli.commands.kb._ensure_memory_backend",
                        _async_return(None))


def test_kb_list_json(kb_stubbed, monkeypatch):
    from cli.commands.kb import kb
    monkeypatch.setattr("modules.memory.registry.kb_list_sources",
                        _async_return(["docs/a.md", "docs/b.md"]))
    res = _run(kb, ["list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == ["docs/a.md", "docs/b.md"]


def test_kb_search_json(kb_stubbed, monkeypatch):
    from cli.commands.kb import kb
    monkeypatch.setattr("modules.memory.registry.kb_search",
                        _async_return("chunk one\nchunk two"))
    res = _run(kb, ["search", "hello", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload == {"query": "hello", "collection": "default",
                       "result": "chunk one\nchunk two"}


def test_surface_list_status_renders_all_known_surfaces(tmp_path, monkeypatch):
    """030 WS-G3: `surface list --status` — the durable-store health view."""
    import json as _json

    from click.testing import CliRunner

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OWNER_SLACK_ID", "C0FFEE")
    from cli.commands.surface import surface
    res = CliRunner().invoke(surface, ["list", "--status", "--json"])
    assert res.exit_code == 0, res.output
    rows = _json.loads(res.stdout if hasattr(res, "stdout") else res.output)
    ids = {r["surface_id"] for r in rows}
    assert {"telegram", "email", "slack", "discord", "whatsapp", "x", "signal"} <= ids
    slack = next(r for r in rows if r["surface_id"] == "slack")
    assert slack["owner_address_configured"] is True
