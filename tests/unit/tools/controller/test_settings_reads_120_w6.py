"""120 W6 — the agent reads its settings as the owner seats do, and its one write is
`unset` with the refusals of `set`.

- `preferences` gains search / changed / check (reads, through core.config_service)
  and unset (a SAFE preference only; a guarded one only tightens; an env flag is never
  written from a turn);
- the chat `/config search` and `/config check` texts live in core and the Telegram seat
  prints them; `polyrob config list --changed` reads core.config_service.is_changed;
- `tool_search` falls back to the curated pack index and names each pack's state.

Every test runs in a temp home: no network, no real env file, no credential.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller

SECRET = "fixture-value-w6-0000-not-a-real-key"
UID = "tenant-w6"


@pytest.fixture(autouse=True)
def _temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("core.instance.is_owner_uid", lambda *a, **k: True)   # the owner's tenant
    (tmp_path / "home").mkdir()
    (tmp_path / "data").mkdir()
    yield


def _env_file():
    from core.config_service import home_env_path
    return home_env_path()


def _home():
    from core.runtime_paths import prefs_home_dir
    return prefs_home_dir()


def _prefs_file():
    from core.prefs import preferences_path
    return preferences_path(_home(), UID)


def _action(monkeypatch, tmp_path, *, writes=True):
    monkeypatch.setenv("PREFS_TOOL_ENABLED", "true" if writes else "false")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("w6-test")
    c.registry = Registry()
    c.user_id = UID
    c.session_id = "s1"
    c.container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    c._register_preferences_action()
    return c.registry.registry.actions["preferences"]


def _ctx(turn_kind=None):
    return types.SimpleNamespace(user_id=UID, is_sub_agent=False, role="orchestrator",
                                 session_id="s1",
                                 metadata={"turn_kind": turn_kind} if turn_kind else {})


async def _run(action, **kw):
    return await action.function(action.param_model(**kw), execution_context=_ctx())


# --------------------------------------------------------------------------- reads

@pytest.mark.asyncio
async def test_reads_register_without_the_write_flag(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, writes=False)
    for op in ("search", "changed", "check"):
        action.param_model(operation=op)
    with pytest.raises(Exception):
        action.param_model(operation="unset", key="goals.daily_quota")


@pytest.mark.asyncio
async def test_search_prints_a_secret_as_set_never_its_value(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="search", query="OPENAI_API_KEY")
    assert res.error is None
    assert "OPENAI_API_KEY" in res.extracted_content
    assert SECRET not in res.extracted_content
    assert SECRET[:12] not in res.extracted_content
    assert "operation='explain'" in res.extracted_content


@pytest.mark.asyncio
async def test_search_text_is_the_telegram_text(monkeypatch, tmp_path):
    from core import config_service
    from surfaces.telegram.prefs_ops import _config_reply
    seat = _config_reply(UID, str(_home()), "default", ["search", "quota"])
    core_text = config_service.search_reply("quota", user_id=UID, home_dir=_home())
    assert seat == core_text
    assert "goals.daily_quota" in seat


@pytest.mark.asyncio
async def test_search_without_words_refuses(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="search")
    assert res.error and "query" in res.error


@pytest.mark.asyncio
async def test_search_failure_is_named_not_no_match(monkeypatch, tmp_path):
    from core import config_service

    def _boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(config_service, "search", _boom)
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="search", query="quota")
    assert "could not search" in res.extracted_content
    assert "No setting" not in res.extracted_content


@pytest.mark.asyncio
async def test_changed_lists_a_written_pref_and_an_env_flag(monkeypatch, tmp_path):
    from core.prefs import write_preference
    ok, err = write_preference(_home(), UID, "goals.daily_quota", 7)
    assert ok, err
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="changed")
    text = res.extracted_content
    assert "goals.daily_quota = 7" in text
    assert "OPENAI_API_KEY" in text and SECRET not in text
    # a posture member at its no-bundle default is not "changed"
    assert "default (no bundle)" not in text


@pytest.mark.asyncio
async def test_changed_names_an_unread_key_and_an_unreadable_file(monkeypatch, tmp_path):
    from core import config_service
    from core.prefs import load_preferences, write_preference
    ok, _ = write_preference(_home(), UID, "goals.daily_quota", 7)
    assert ok
    path = _prefs_file()
    path.write_text("[goals\nbroken = ")  # unreadable TOML
    load_preferences(_home(), UID)  # a read marks it unreadable (the last good copy stays)
    real = config_service.describe

    def _describe(key, **kw):
        if key == "goals.max_concurrent":
            raise OSError("store")
        return real(key, **kw)
    monkeypatch.setattr(config_service, "describe", _describe)
    text = config_service.changed_reply(user_id=UID, home_dir=_home())
    assert "UNREADABLE" in text
    assert "could not be read: goals.max_concurrent" in text


@pytest.mark.asyncio
async def test_check_names_keys_never_values(monkeypatch, tmp_path):
    env = _env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"AUTONOMY_MODE={SECRET}\nCODE_EXEC_ENABLE=true\n")
    path = _prefs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[goals]\ndaily_quota = "{SECRET}"\n')
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="check")
    text = res.extracted_content
    assert "AUTONOMY_MODE" in text and "CODE_EXEC_ENABLE" in text
    assert "goals.daily_quota" in text
    assert SECRET not in text and SECRET[:12] not in text


def test_owner_check_no_longer_echoes_an_enum_value(tmp_path):
    from core.prefs import check_env_files
    env = tmp_path / ".env"
    env.write_text(f"AUTONOMY_MODE={SECRET}\n")
    findings = check_env_files([env])
    assert findings and "supervised" in findings[0]
    assert SECRET not in "\n".join(findings)


@pytest.mark.asyncio
async def test_check_that_did_not_run_is_not_no_issues(monkeypatch, tmp_path):
    from core import config_service

    def _boom(**k):
        raise OSError("disk")
    monkeypatch.setattr(config_service, "check_findings", _boom)
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation="check")
    assert "could not run the check" in res.extracted_content
    assert "no issues" not in res.extracted_content


def test_check_text_is_the_telegram_text(monkeypatch, tmp_path):
    from core import config_service
    from surfaces.telegram.prefs_ops import _config_reply
    _env_file().write_text("CODE_EXEC_ENABLE=true\n")
    assert _config_reply(UID, str(_home()), "default", ["check"]) == config_service.check_reply(
        user_id=UID, home_dir=_home())


# --------------------------------------------------------------------------- unset

@pytest.mark.asyncio
async def test_unset_removes_a_safe_preference(monkeypatch, tmp_path):
    from core.prefs import load_preferences, write_preference
    ok, _ = write_preference(_home(), UID, "goals.daily_quota", 7)
    assert ok
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key="goals.daily_quota")
    assert res.error is None, res.error
    assert res.extracted_content.startswith("removed goals.daily_quota: effective")
    assert "goals.daily_quota" not in load_preferences(_home(), UID)


@pytest.mark.asyncio
async def test_unset_of_a_guarded_preference_is_refused(monkeypatch, tmp_path):
    from core.prefs import load_preferences, write_preference
    ok, _ = write_preference(_home(), UID, "budget.wallet_daily_usd", 5.0)
    assert ok
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key="budget.wallet_daily_usd")
    assert res.error and "guarded" in res.error and "only tightens" in res.error
    assert load_preferences(_home(), UID)["budget.wallet_daily_usd"] == 5.0


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "AUTONOMY_MODE", "CODE_EXEC_ENABLED"])
@pytest.mark.asyncio
async def test_unset_of_an_env_flag_is_never_written_from_a_turn(monkeypatch, tmp_path, key):
    env = _env_file()
    env.write_text(f"{key}=x\n")
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key=key)
    assert res.error and "Nothing was changed" in res.error
    assert env.read_text() == f"{key}=x\n"


@pytest.mark.asyncio
async def test_unset_on_a_forged_turn_is_refused(monkeypatch, tmp_path):
    from core.prefs import load_preferences, write_preference
    ok, _ = write_preference(_home(), UID, "goals.daily_quota", 7)
    assert ok
    action = _action(monkeypatch, tmp_path)
    res = await action.function(action.param_model(operation="unset", key="goals.daily_quota"),
                                execution_context=_ctx("self_wake"))
    assert res.error and "unset is refused" in res.error
    assert load_preferences(_home(), UID)["goals.daily_quota"] == 7


@pytest.mark.asyncio
async def test_unset_of_a_key_not_set_says_so(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key="goals.daily_quota")
    assert res.error and "not set" in res.error


@pytest.mark.asyncio
async def test_unset_of_a_room_key_names_the_room_seat(monkeypatch, tmp_path):
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key="chat.mode")
    assert res.error and "/groups set" in res.error


@pytest.mark.asyncio
async def test_unset_refuses_an_unreadable_file_and_leaves_it(monkeypatch, tmp_path):
    from core.prefs import write_preference
    ok, _ = write_preference(_home(), UID, "goals.daily_quota", 7)
    assert ok
    path = _prefs_file()
    path.write_text("[goals\nbroken = ")
    action = _action(monkeypatch, tmp_path)
    res = await _run(action, operation="unset", key="goals.daily_quota")
    assert res.error and "unreadable" in res.error
    assert path.read_text() == "[goals\nbroken = "


# --------------------------------------------------------------------------- seats

def test_cli_list_changed_reads_the_core_rule(monkeypatch, tmp_path):
    import cli.commands.config as cfg
    from core import config_service
    assert not hasattr(cfg, "_is_changed")
    member = types.SimpleNamespace(source="default (no bundle)")
    assert not config_service.is_changed(member)
    assert not config_service.is_changed(types.SimpleNamespace(source="built-in"))
    assert config_service.is_changed(types.SimpleNamespace(source="env:shell"))
    assert config_service.is_changed(types.SimpleNamespace(source="pref"))


# --------------------------------------------------------------------------- packs

_ROW = {"id": "widgets", "dist": "polyrob-widgets", "version": "1.0.0",
        "tier": "third-party", "summary": "draws widgets", "capabilities": ["network"]}


def test_tool_search_falls_back_to_the_pack_index(monkeypatch):
    from core.packs import index, state
    from tools import tool_search
    monkeypatch.setattr(index, "search", lambda q="", **kw: [_ROW] if "widget" in q else [])
    monkeypatch.setattr(index, "killed", lambda *a: None)
    monkeypatch.setattr(state, "record", lambda pid: None)
    lines = tool_search.pack_index_lines("big widget")
    assert lines[0] == "The pack index:"
    assert lines[1] == (f"- pack {index.row_line(_ROW)} [not installed — the owner installs "
                        "it (polyrob pack install widgets)]")


def test_tool_search_names_an_unreadable_index(monkeypatch):
    from core.packs import index
    from tools import tool_search

    def _boom(q="", **kw):
        raise ValueError("bad json")
    monkeypatch.setattr(index, "search", _boom)
    assert tool_search.pack_index_lines("widget") == [
        "The pack index could not be read (ValueError)."]


def test_tool_search_says_no_pack_matches(monkeypatch):
    from core.packs import index
    from tools import tool_search
    monkeypatch.setattr(index, "search", lambda q="", **kw: [])
    assert tool_search.pack_index_lines("zzz") == ["No pack in the index matches 'zzz'."]


def test_tool_search_names_an_index_file_that_cannot_be_read(tmp_path, monkeypatch):
    """120 W9 leftover: the real reader, not a stub: a broken index.json is named."""
    from core.packs import index
    from tools import tool_search
    bad = tmp_path / "index.json"
    bad.write_text("{not json")
    monkeypatch.setattr(index, "INDEX_FILE", bad)
    assert tool_search.pack_index_lines("zzz") == [
        "The pack index could not be read (PackIndexError)."]


@pytest.mark.asyncio
async def test_tool_search_action_appends_the_packs_only_when_nothing_matches(monkeypatch):
    from core.packs import index
    from tools import tool_search
    monkeypatch.setattr(tool_search, "pack_index_lines", lambda q: ["PACKS"])
    ctl = types.SimpleNamespace(container=None, list_tools=lambda: [],
                                iter_mcp_tools_metadata=lambda: [], _is_sub_agent=False)
    monkeypatch.setattr(tool_search, "disclosure_ceiling", lambda c: None)
    monkeypatch.setattr(tool_search, "seat_probe", lambda c: None)
    res = await tool_search.perform_tool_search(ctl, "qqqzzznothing")
    assert res.extracted_content.endswith("PACKS")
    assert index  # imported for the monkeypatch target only


def test_pack_cli_prints_the_core_row_line(monkeypatch):
    from click.testing import CliRunner

    from cli.commands import pack as pack_cmd
    from core.packs import index
    monkeypatch.setattr(index, "search", lambda q="", **kw: [_ROW])
    monkeypatch.setattr(index, "killed", lambda *a: "withdrawn")
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda: None)
    out = CliRunner().invoke(pack_cmd.pack, ["search", "widget"]).output
    assert out.strip() == index.row_line(_ROW)
    assert out.strip().endswith("· ! withdrawn")


# --------------------------------------------------------------------------- the owner's identifiers

@pytest.mark.asyncio
@pytest.mark.parametrize("op, kw", [("changed", {}), ("search", {"query": "owner"}),
                                    ("explain", {"key": "POLYROB_OWNER_TELEGRAM_ID"})])
async def test_an_owner_identifier_is_shown_only_as_set(monkeypatch, tmp_path, op, kw):
    """The model reaches the owner by target='owner', never by the raw value."""
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "900000001")
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.invalid")
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation=op, **kw)
    assert res.error is None, res.error
    assert "POLYROB_OWNER_TELEGRAM_ID" in res.extracted_content
    assert "900000001" not in res.extracted_content
    assert "owner@example.invalid" not in res.extracted_content


def test_only_identifier_keys_are_masked_as_owner_identifiers():
    from tools.controller.preferences_action import _is_owner_identifier
    for key in ("POLYROB_OWNER_TELEGRAM_ID", "POLYROB_OWNER_EMAIL", "OWNER_DISCORD_ID",
                "OWNER_WALLET_ADDRESSES", "POLYROB_OWNER_USERNAME", "POLYROB_LOCAL_OWNER",
                "POLYROB_OWNER_USER_ID"):
        assert _is_owner_identifier(key), key
    for key in ("OWNER_DIGEST_ENABLED", "OWNER_SURFACE", "OWNER_THREAD_TAIL_HOURS",
                "TELEGRAM_BOT_USERNAME", "DEFAULT_MODEL"):
        assert not _is_owner_identifier(key), key


@pytest.mark.asyncio
@pytest.mark.parametrize("op, kw", [("changed", {}), ("search", {"query": "model"}), ("check", {})])
async def test_the_operator_reads_refuse_a_tenant_that_is_not_the_owner(monkeypatch, tmp_path, op, kw):
    monkeypatch.setattr("core.instance.is_owner_uid", lambda *a, **k: False)
    action = _action(monkeypatch, tmp_path, writes=False)
    res = await _run(action, operation=op, **kw)
    assert res.error and "the owner's" in res.error
