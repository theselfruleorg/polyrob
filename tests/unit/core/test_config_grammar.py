"""113 W4 item 4 (D-3): one `/config` grammar on every seat.

``core.config_service.parse_config_command`` parses the words; the chat apps
(``surfaces.telegram.prefs_ops._config_reply``), the terminal
(``cli.ui.commands.h_config.cmd_config``) and ``polyrob config`` know the same
seven verbs, and ``unset`` exists on every seat (it runs on the box only).
"""
import pytest

from core.config_service import (CONFIG_USAGE, CONFIG_VERBS, parse_config_command,
                                 unset_value)


def test_no_words_is_list():
    assert parse_config_command([]).verb == "list"
    assert parse_config_command(["list", "style"]).query == "style"


@pytest.mark.parametrize("verb", ["get", "explain", "unset"])
def test_a_key_verb_needs_its_key(verb):
    assert parse_config_command([verb]).error == f"Usage: /config {verb} <key>"
    cmd = parse_config_command([verb, "style.tone"])
    assert (cmd.verb, cmd.key, cmd.error) == (verb, "style.tone", "")


def test_set_strips_the_flags_from_the_value():
    cmd = parse_config_command(["set", "style.tone", "warm", "and", "brief", "--confirm"])
    assert (cmd.key, cmd.value, cmd.confirm) == ("style.tone", "warm and brief", True)
    assert parse_config_command(["set", "style.tone"]).error.startswith("Usage: /config set")
    assert parse_config_command(["set", "k", "--confirm"]).error


def test_search_joins_every_word():
    assert parse_config_command(["search", "daily", "cap"]).query == "daily cap"
    assert parse_config_command(["search"]).error


def test_an_unknown_word_names_the_one_usage():
    cmd = parse_config_command(["frobnicate"])
    assert CONFIG_USAGE in cmd.error
    for verb in CONFIG_VERBS:
        assert verb in CONFIG_USAGE


def test_polyrob_config_has_every_verb_of_the_grammar():
    from cli.commands.config import config
    assert set(CONFIG_VERBS) <= set(config.commands)


def test_the_terminal_and_the_chat_apps_refuse_the_same_bad_line(tmp_path):
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    from surfaces.telegram.prefs_ops import _config_reply
    for words in (["frobnicate"], ["get"], ["set", "k"], ["search"]):
        expected = parse_config_command(words).error
        assert cmd_config(ConfigCtx(user_id="u1", home_dir=tmp_path), words) == expected
        assert _config_reply("u1", str(tmp_path), "i1", words) == expected


def test_unset_is_refused_on_a_remote_seat_and_names_the_box():
    res = unset_value("CODE_EXEC_ENABLED", surface="telegram")
    assert not res.ok and "polyrob config unset CODE_EXEC_ENABLED" in res.message


def test_a_preference_has_no_unset():
    res = unset_value("style.verbosity")
    assert not res.ok and "/config set style.verbosity" in res.message


def test_the_terminal_unset_removes_the_line(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CODE_EXEC_ENABLED=true\nOTHER=1\n")
    monkeypatch.setattr("core.config_service.home_env_path", lambda: env)
    from cli.ui.commands.h_config import ConfigCtx, cmd_config
    out = cmd_config(ConfigCtx(user_id="u1", home_dir=tmp_path), ["unset", "CODE_EXEC_ENABLED"])
    assert out.startswith("Removed CODE_EXEC_ENABLED")
    assert "CODE_EXEC_ENABLED" not in env.read_text() and "OTHER=1" in env.read_text()
    again = cmd_config(ConfigCtx(user_id="u1", home_dir=tmp_path), ["unset", "CODE_EXEC_ENABLED"])
    assert again.startswith("error:") and "is not set" in again


def test_chat_search_and_check_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path))
    from surfaces.telegram.prefs_ops import _config_reply
    assert "style.verbosity" in _config_reply("u1", str(tmp_path), "i1", ["search", "verbosity"])
    assert _config_reply("u1", str(tmp_path), "i1", ["check"]).startswith("Config check:")
