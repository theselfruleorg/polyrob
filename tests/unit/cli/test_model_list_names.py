"""Behavioral tests for `polyrob model list` — P1.2: print real model NAMES (not just
a misleading count), while PRESERVING the provider ready/no-key status table.
"""
from click.testing import CliRunner

from cli.commands.model import model


def test_model_list_prints_real_model_names(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    out = CliRunner().invoke(model, ["list"]).output
    assert "glm" in out.lower() or "grok" in out.lower(), "must print real model names"
    assert "models)" not in out, "the misleading '(N models)' count must be gone"


def test_model_list_still_shows_provider_status(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    out = CliRunner().invoke(model, ["list"]).output
    assert "openrouter" in out.lower()   # status table preserved
