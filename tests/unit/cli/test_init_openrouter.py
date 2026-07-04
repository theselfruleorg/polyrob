# tests/unit/cli/test_init_openrouter.py
from click.testing import CliRunner
from cli.commands.init import init_cmd


def test_init_prompts_openrouter_first(monkeypatch, tmp_path):
    """The interactive key section leads with OpenRouter and offers all providers."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path / ".polyrob")
    runner = CliRunner()
    # Feed: openrouter key, then blank for every other prompt.
    result = runner.invoke(init_cmd, ["--quick"], input="sk-or-test\n\n\n\n\n\n\n")
    assert result.exit_code == 0
    out = result.output.lower()
    assert "openrouter" in out
    # OpenRouter must be mentioned before Anthropic/OpenAI in the prompt flow.
    assert out.index("openrouter") < out.index("anthropic")


def test_init_writes_openrouter_key(monkeypatch, tmp_path):
    home = tmp_path / ".polyrob"
    monkeypatch.setattr("core.paths.polyrob_home", lambda: home)
    runner = CliRunner()
    result = runner.invoke(init_cmd, ["--quick"], input="sk-or-xyz\n\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    env_text = (home / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-xyz" in env_text


def test_init_bare_min_satisfied_by_openrouter(monkeypatch, tmp_path):
    """No 'no LLM key' warning when only OpenRouter is provided."""
    home = tmp_path / ".polyrob"
    monkeypatch.setattr("core.paths.polyrob_home", lambda: home)
    runner = CliRunner()
    result = runner.invoke(init_cmd, ["--quick"], input="sk-or-only\n\n\n\n\n\n\n")
    assert "No LLM API key" not in result.output
