"""The first ten minutes: what a new user reads, and what they do not lose."""
import logging

import pytest
from click.testing import CliRunner

from cli.commands.init import init_cmd
from cli.polyrob import _console_log_level, cli
from cli.ui.log_squelch import _ProviderAuthFilter


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "NVIDIA_API_KEY", "DEEPSEEK_API_KEY", "DEFAULT_MODEL", "DEFAULT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.fixture
def probe(monkeypatch):
    """What the provider says about a key. No network."""
    answer = {"verdict": True}
    monkeypatch.setattr("cli.commands.auth._stdin_isatty", lambda: True)
    monkeypatch.setattr("cli.commands.auth._probe_key",
                        lambda spec, key: (answer["verdict"], "stub"))
    return answer


KEY = "sk-or-v1-" + "a" * 40


def _setup(answers):
    return CliRunner().invoke(init_cmd, ["--quick"], input="\n".join(answers) + "\n")


def test_quick_setup_counts_its_own_sections(home, probe):
    result = _setup(["openrouter", KEY, "n", ""])
    assert "Section 1/2" in result.output and "Section 2/2" in result.output
    assert "/7" not in result.output


def test_setup_ends_on_how_to_start_not_on_a_posture_table(home, probe):
    result = _setup(["openrouter", KEY, "n", ""])
    assert result.exit_code == 0, result.output
    assert "Start now:  polyrob" in result.output
    assert "frozen at import" not in result.output
    assert "knobs:" not in result.output


def test_scripted_setup_still_prints_the_posture_card(home):
    result = CliRunner().invoke(init_cmd, ["--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "Effective posture" in result.output


def test_setup_with_no_key_does_not_say_start_now(home, probe):
    result = _setup(["skip", ""])
    assert "Start now" not in result.output
    assert "polyrob auth add openrouter" in result.output


def test_a_rejected_key_is_named_and_not_saved(home, probe):
    probe["verdict"] = False
    result = _setup(["openrouter", KEY, "n", "skip", ""])
    assert "rejected this key" in result.output
    env_file = home / ".env"
    assert not env_file.exists() or KEY not in env_file.read_text()


def test_a_rejected_key_can_be_kept_on_purpose(home, probe):
    probe["verdict"] = False
    _setup(["openrouter", KEY, "y", "skip", ""])
    assert KEY in (home / ".env").read_text()


def test_an_unchecked_key_is_saved_and_does_not_claim_it_works(home, probe):
    probe["verdict"] = None
    result = _setup(["openrouter", KEY, "n", ""])
    assert KEY in (home / ".env").read_text()
    assert "could not check" in result.output
    assert "The key works" not in result.output


def test_ctrl_c_keeps_a_pasted_key_and_says_how_to_go_on(home, probe, monkeypatch):
    # The question after the key aborts, as Ctrl-C does. Raised by hand: whether the
    # runner aborts at the end of the input depends on the click version.
    import click
    import cli.commands.init as init_mod

    def ctrl_c(*a, **k):
        raise click.Abort()

    monkeypatch.setattr(init_mod.click, "confirm", ctrl_c)
    result = CliRunner().invoke(init_cmd, ["--quick"], input=f"openrouter\n{KEY}\n")
    assert KEY in (home / ".env").read_text()
    assert "Setup stopped" in result.output
    assert "polyrob setup" in result.output
    assert result.exit_code == 130


def test_a_model_with_no_known_provider_is_not_saved_as_if_it_worked(home, probe):
    result = _setup(["skip", "not-a-real-model-name"])
    assert "did not save it" in " ".join(result.output.split())
    env_file = home / ".env"
    assert not env_file.exists() or "not-a-real-model-name" not in env_file.read_text()


def test_a_rerun_offers_to_keep_the_connected_provider(home, probe):
    (home / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    result = CliRunner().invoke(init_cmd, ["--quick"], input="\n\n")
    assert "Already connected: openrouter" in result.output


def test_openrouter_is_probed_where_a_dead_key_fails(monkeypatch):
    """/models answers 200 to anybody there, so it called a dead key "verified"."""
    from cli.commands.auth import _probe_key
    from modules.llm.provider_spec import get_spec
    seen = {}

    class _Resp:
        status_code = 401
        text = "User not found."

    def fake_get(url, **_kw):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr("httpx.get", fake_get)
    verdict, _ = _probe_key(get_spec("openrouter"), KEY)
    assert seen["url"].endswith("/key")
    assert verdict is False


def test_nvidia_is_not_called_verified_by_a_public_model_list(monkeypatch):
    from cli.commands.auth import _probe_key
    from modules.llm.provider_spec import get_spec
    monkeypatch.setattr("httpx.get", lambda *_a, **_k: pytest.fail("no call can test this key"))
    verdict, detail = _probe_key(get_spec("nvidia"), KEY)
    assert verdict is None and "public" in detail


def test_usage_lines_name_the_command_people_type():
    result = CliRunner().invoke(cli, ["--help"], prog_name="polyrob")
    assert result.output.startswith("Usage: polyrob")


def test_main_passes_the_program_name():
    from pathlib import Path
    src = Path(__file__).resolve().parents[3].joinpath("cli", "polyrob.py").read_text()
    assert 'cli(prog_name="polyrob")' in src


def test_status_is_the_doctor():
    result = CliRunner().invoke(cli, ["status", "--help"], prog_name="polyrob")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("argv,level", [
    (["gateway"], "INFO"), (["telegram", "--token", "x"], "INFO"), (["serve"], "INFO"),
    (["skills", "list"], "WARNING"), (["dashboard"], "WARNING"), ([], "WARNING"),
    (["--plain", "gateway"], "INFO"),
])
def test_only_a_daemon_prints_info_lines(argv, level):
    assert _console_log_level(argv) == level


@pytest.mark.parametrize("message,shown", [
    ("OpenRouter tool generation failed: Error code: 401 - {'error': 'User not found.'}", False),
    ("❌ No fallback provider available after LLMAuthenticationError from openrouter", False),
    ("Validation failed for done. Tool calls MUST be normalized", False),
    ("Available actions (first 20): ['send_message']", False),
    ("database is locked", True),
    ("Tool email failed: connection refused", True),
])
def test_the_terminal_keeps_real_errors_and_drops_the_retry_noise(message, shown):
    record = logging.LogRecord("task.executor", logging.ERROR, __file__, 1, message, None, None)
    assert _ProviderAuthFilter().filter(record) is shown


def test_gateway_with_nothing_enabled_starts_nothing(home, monkeypatch):
    import asyncio

    from cli.commands import gateway as gw
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda interactive=False: True)
    monkeypatch.setattr(gw, "_enabled_specs", lambda skip=frozenset(): [])
    monkeypatch.setattr(gw, "_withheld_enabled", lambda skip=frozenset(): [])
    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container",
                        lambda **_k: pytest.fail("no runtime for an empty gateway"))
    messages = []
    monkeypatch.setattr(gw, "_no_surfaces_message", lambda: messages.append(1))
    assert asyncio.run(gw._run_gateway(0, None, False)) is None
    assert messages == [1]


def test_help_fits_inside_its_panel(monkeypatch):
    from cli.ui.commands import h_help
    monkeypatch.setenv("COLUMNS", "80")
    assert h_help._width() == 76
