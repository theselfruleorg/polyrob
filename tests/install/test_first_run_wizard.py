"""First-run wizard + REPL credential reachability (027 WP4b).

The old Section-1 wizard prompted for SIX provider keys in a row, gave zero
feedback on what you pasted, and a completed OAuth connect was unreadable
without a flag no surface named. A one-shot `polyrob run` user got the same
six-prompt wall.
"""

import types

from click.testing import CliRunner


def _run_wizard(input_text: str):
    from cli.commands.init import _prompt_provider_keys

    runner = CliRunner()
    collected: dict = {}
    with runner.isolation(input=input_text) as outstreams:
        try:
            _prompt_provider_keys(collected)
        except Exception:
            pass
        output = outstreams[0].getvalue().decode("utf-8", "replace")
    return collected, output


class TestSingleProviderWizard:
    def test_blank_default_connects_openrouter_with_one_key_prompt(self):
        collected, output = _run_wizard("\nsk-or-v1-abcdefghijklmnopqrstuvwxyz\nn\n")
        assert collected.get("OPENROUTER_API_KEY", "").startswith("sk-or-v1-")
        assert "Anthropic API key" not in output, (
            "one provider choice + one key prompt — never six sequential prompts"
        )

    def test_pick_provider_by_name(self):
        collected, output = _run_wizard(
            "anthropic\nsk-ant-abcdefghijklmnopqrstuvwxyz123\nn\n"
        )
        assert "ANTHROPIC_API_KEY" in collected

    def test_skip_answers_nothing(self):
        collected, _ = _run_wizard("skip\n")
        assert collected == {}

    def test_entered_key_gets_doctor_feedback(self):
        # A 5-char paste is malformed — the wizard must say so immediately,
        # like `polyrob auth add` does, not accept it silently.
        _, output = _run_wizard("\nshort\nn\n")
        assert "unusable" in output or "malformed" in output


class TestOAuthStoreUsableUnderLocalMode:
    def test_local_mode_defaults_store_rungs_on(self, monkeypatch):
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        monkeypatch.delenv("LLM_AUTH_STORE_ENABLED", raising=False)
        from core.llm_auth.resolve import _store_rungs_allowed

        assert _store_rungs_allowed() is True, (
            "an `auth add <oauth-seat>` connect on a local box must be readable "
            "without a second undocumented flag"
        )

    def test_explicit_off_wins(self, monkeypatch):
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "false")
        from core.llm_auth.resolve import _store_rungs_allowed

        assert _store_rungs_allowed() is False


class TestModelSetDefaultZeroKey:
    def test_cancelled_picker_with_no_keys_names_the_connect_verbs(self, monkeypatch):
        import cli.ui.model_selector as selector

        monkeypatch.setattr(selector, "run_standalone", lambda: None)
        for var in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                    "GEMINI_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY"):
            monkeypatch.delenv(var, raising=False)

        from cli.commands.model import model as model_group

        result = CliRunner().invoke(model_group, ["set-default"],
                                    catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "auth add" in result.output, (
            "with zero keys the picker has nothing to pick — say how to connect "
            "a provider instead of bare 'Cancelled.'"
        )


class TestReplSlashes:
    def test_auth_and_doctor_slash_commands_exist(self):
        from cli.ui.commands.handlers import build_default_registry

        registry = build_default_registry()
        names = set(registry.names()) if hasattr(registry, "names") else set()
        if not names:
            names = {c.name for c in getattr(registry, "commands", lambda: [])()} \
                if callable(getattr(registry, "commands", None)) else set()
        if not names:
            names = set(getattr(registry, "_commands", {}).keys())
        assert "auth" in names, "/auth must exist so an expired key is fixable in-session"
        assert "doctor" in names, "/doctor must exist for in-session health checks"
