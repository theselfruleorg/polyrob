"""030 WS-F3 / 026 C4 — `polyrob config get|list|search|explain` (CLI/REPL parity).

The REPL `/config` has had these read verbs since 018 P2a; the CLI seat only
had set/unset/show/path/check. These tests pin the new read-only verbs: all
route through core.config_service (masking + provenance are the service's),
none preflight an LLM key, and each takes --json.

Isolation mirrors tests/unit/cli/test_config_cmd.py: HOME + CWD at tmp so no
read ever touches the developer's real config.
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", True)
    return home, proj


def _runner():
    from cli.commands.config import config as config_group
    return CliRunner(), config_group


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_flag_shows_value_source_and_kind(isolated, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["get", "GOAL_DAILY_QUOTA"])
    assert res.exit_code == 0, res.output
    assert "GOAL_DAILY_QUOTA" in res.output
    assert "9" in res.output
    assert "env" in res.output           # source
    assert "kind: int" in res.output


def test_get_masks_secret_flag_value(isolated, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:live-secret-token")
    runner, config = _runner()
    res = runner.invoke(config, ["get", "TELEGRAM_BOT_TOKEN"])
    assert res.exit_code == 0, res.output
    assert "live-secret-token" not in res.output
    assert "(set, masked)" in res.output


def test_get_pref_key_reports_pref_namespace(isolated, tmp_path):
    runner, config = _runner()
    res = runner.invoke(config, ["get", "style.verbosity",
                                 "--user", "u1", "--home", str(tmp_path / "dh")])
    assert res.exit_code == 0, res.output
    assert "style.verbosity" in res.output
    assert "namespace: pref" in res.output


def test_get_unknown_key_fails_with_suggestion(isolated):
    runner, config = _runner()
    res = runner.invoke(config, ["get", "GOAL_DAILY_QOUTA"])
    assert res.exit_code != 0
    assert "unknown key" in res.output
    assert "GOAL_DAILY_QUOTA" in res.output  # closest-match hint


def test_get_json(isolated, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["get", "GOAL_DAILY_QUOTA", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["key"] == "GOAL_DAILY_QUOTA"
    assert payload["value"] == 9
    assert payload["source"] == "env"
    assert payload["namespace"] == "flag"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_group_filter_shows_only_that_group(isolated):
    from core.flags import REGISTRY
    group = REGISTRY["GOALS_ENABLED"].group
    runner, config = _runner()
    res = runner.invoke(config, ["list", "--group", group])
    assert res.exit_code == 0, res.output
    assert "GOALS_ENABLED" in res.output
    assert "style.verbosity" not in res.output  # prefs are a different group


def test_list_changed_shows_only_non_default_values(isolated, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["list", "--changed"])
    assert res.exit_code == 0, res.output
    assert "GOAL_DAILY_QUOTA" in res.output
    # an unset enum flag resolves from its default -> excluded
    assert "TOOL_SCHEMA_ERROR_POLICY" not in res.output


def test_list_json(isolated, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["list", "--changed", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert any(row["key"] == "GOAL_DAILY_QUOTA" for row in payload)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_finds_flag_by_name(isolated):
    runner, config = _runner()
    res = runner.invoke(config, ["search", "autonomy_enabled"])
    assert res.exit_code == 0, res.output
    assert "AUTONOMY_ENABLED" in res.output


def test_search_no_hits_is_not_an_error(isolated):
    runner, config = _runner()
    res = runner.invoke(config, ["search", "zzz_no_such_setting_zzz"])
    assert res.exit_code == 0, res.output
    assert "no settings matching" in res.output


def test_search_json(isolated):
    runner, config = _runner()
    res = runner.invoke(config, ["search", "autonomy_enabled", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert any(row["key"] == "AUTONOMY_ENABLED" for row in payload)


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------

def test_explain_shows_full_provenance_chain(isolated, monkeypatch):
    _home, proj = isolated
    (proj / ".polyrob").mkdir(exist_ok=True)
    (proj / ".polyrob" / ".env").write_text("GOAL_DAILY_QUOTA=7\n")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["explain", "GOAL_DAILY_QUOTA"])
    assert res.exit_code == 0, res.output
    assert "provenance" in res.output
    assert "env:process" in res.output       # the running process's value
    assert "env-file" in res.output          # the file that also holds it
    assert "built-in:catalog" in res.output  # the documented default


def test_explain_unknown_key_fails(isolated):
    runner, config = _runner()
    res = runner.invoke(config, ["explain", "NOT_A_REAL_KEY_AT_ALL"])
    assert res.exit_code != 0
    assert "unknown key" in res.output


def test_explain_json_carries_the_chain(isolated, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "9")
    runner, config = _runner()
    res = runner.invoke(config, ["explain", "GOAL_DAILY_QUOTA", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    origins = [rung["origin"] for rung in payload["chain"]]
    assert "env:process" in origins
    assert "built-in:catalog" in origins


# ---------------------------------------------------------------------------
# read verbs never write
# ---------------------------------------------------------------------------

def test_read_verbs_do_not_create_env_files(isolated, monkeypatch):
    _home, proj = isolated
    runner, config = _runner()
    for args in (["get", "GOALS_ENABLED"], ["list", "--changed"],
                 ["search", "goals"], ["explain", "GOALS_ENABLED"]):
        res = runner.invoke(config, args)
        assert res.exit_code == 0, (args, res.output)
    assert not (proj / ".polyrob" / ".env").exists()
