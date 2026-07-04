"""Behavioral tests for `polyrob model set-default` — P1.3: no-args launches the
interactive picker; two-args stays exactly as before (backward-compatible).
"""
from click.testing import CliRunner

from cli.commands.model import model
import cli.commands.model as mod


def test_set_default_no_args_launches_picker_and_persists(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    monkeypatch.setattr(
        "cli.ui.model_selector.run_standalone", lambda *a, **k: ("openrouter", "z-ai/glm-5.2")
    )
    saved = {}
    monkeypatch.setattr(mod, "set_default_model", lambda p, m: saved.update(p=p, m=m), raising=False)
    # if set_default_model is imported inside the fn, patch cli.config_store.set_default_model instead:
    monkeypatch.setattr("cli.config_store.set_default_model", lambda p, m: saved.update(p=p, m=m))
    res = CliRunner().invoke(model, ["set-default"])
    assert res.exit_code == 0 and saved == {"p": "openrouter", "m": "z-ai/glm-5.2"}


def test_set_default_picker_cancel_is_clean(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    monkeypatch.setattr("cli.ui.model_selector.run_standalone", lambda *a, **k: None)
    res = CliRunner().invoke(model, ["set-default"])
    assert res.exit_code == 0 and "cancel" in res.output.lower()


def test_set_default_with_args_still_works(monkeypatch):
    saved = {}
    monkeypatch.setattr("cli.config_store.set_default_model", lambda p, m: saved.update(p=p, m=m))
    res = CliRunner().invoke(model, ["set-default", "openrouter", "z-ai/glm-5.2"])
    assert res.exit_code == 0 and saved == {"p": "openrouter", "m": "z-ai/glm-5.2"}
