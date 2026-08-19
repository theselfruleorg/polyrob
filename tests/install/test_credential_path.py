"""One credential path (proposal 027 WP4).

Four doors with four behaviors confused every fresh install: config set wrote
project scope while auth status recommended it for keys; the zero-key resolver
silently invented ("gemini", None); the no-key message was a six-file wall with
its own remedy grammar; auth status printed a ~33-provider wall.
"""

import types
from pathlib import Path

from click.testing import CliRunner


class TestZeroKeyResolver:
    def test_zero_keys_resolves_to_none_not_gemini(self, monkeypatch, tmp_path):
        for var in ("CHAT_PROVIDER", "CHAT_MODEL", "DEFAULT_PROVIDER", "DEFAULT_MODEL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("POLYROB_CLI_CONFIG", str(tmp_path / "cli.json"))

        from cli.config_store import resolve_provider_model

        provider, model = resolve_provider_model(None, None, available_keys=[])
        assert provider is None and model is None, (
            "a zero-credential box must resolve to (None, None), not a provider "
            "the user never configured"
        )


class TestConfigSetSecretScope:
    def _run_set(self, tmp_path, monkeypatch, args):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("POLYROB_HOME", str(home))
        from cli.commands.config import config as config_group

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(config_group, args, catch_exceptions=False)
            project_env = Path(".polyrob") / ".env"
            project_text = project_env.read_text() if project_env.exists() else ""
        # POLYROB_HOME points at the config-home dir itself (the ~/.polyrob
        # equivalent), so the global env file is directly inside it.
        global_env = home / ".env"
        global_text = global_env.read_text() if global_env.exists() else ""
        return result, global_text, project_text

    def test_secret_key_defaults_to_global_scope(self, tmp_path, monkeypatch):
        result, global_text, project_text = self._run_set(
            tmp_path, monkeypatch, ["set", "FOO_API_KEY", "abcdefghijklmnopqrstuvwx"]
        )
        assert result.exit_code == 0, result.output
        assert "FOO_API_KEY" in global_text, (
            "a credential must land in ~/.polyrob/.env by default — a "
            "project-scope key vanishes the moment the user cd's away"
        )
        assert "FOO_API_KEY" not in project_text

    def test_secret_key_project_scope_opt_in(self, tmp_path, monkeypatch):
        result, global_text, project_text = self._run_set(
            tmp_path,
            monkeypatch,
            ["set", "FOO_API_KEY", "abcdefghijklmnopqrstuvwx", "--project"],
        )
        assert result.exit_code == 0, result.output
        assert "FOO_API_KEY" in project_text
        assert "FOO_API_KEY" not in global_text


class TestNoKeyMessageGrammar:
    def test_names_auth_add_and_init_without_the_six_path_wall(self):
        from modules.llm.profiles import no_key_message

        msg = no_key_message()
        assert "polyrob auth add" in msg
        assert "polyrob init" in msg
        assert "config/.env.development" not in msg, (
            "the six-file wall is gone — keys live in ~/.polyrob/.env; the env "
            "ladder belongs in the docs, not the first-run error"
        )


class TestAuthStatusTruncation:
    def test_absent_provider_wall_is_capped(self, monkeypatch):
        import cli.commands.auth as auth_mod
        import modules.llm.profiles as profiles

        def fake_status():
            row = lambda: types.SimpleNamespace(  # noqa: E731
                present=False, usable=False, source="", health="ok",
                expires_at=None, reason="",
            )
            return {f"provider{i:02d}": row() for i in range(30)}

        monkeypatch.setattr(profiles, "credential_status", fake_status)

        runner = CliRunner()
        result = runner.invoke(auth_mod.auth, ["status"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "+24 more" in result.output, (
            "auth status must cap the not-configured list like doctor does "
            "(6 named + N more), not print a 30-name wall"
        )
        assert "provider29" not in result.output
        assert "polyrob auth add" in result.output
