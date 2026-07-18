"""Argument tab-completion for the POLYROB CLI slash-completer.

Covers the `/model`, `/toolset`, and `/persona` argument branches added to
``SlashCompleter.get_completions`` — plus a regression guard that the existing
command-name and `/replay` session-id completion still work.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.ui.commands import (
    Command,
    CommandRegistry,
    SlashCompleter,
    build_default_registry,
)


class _Doc:
    """Minimal stand-in for prompt_toolkit's Document (only what the completer reads)."""

    def __init__(self, text: str) -> None:
        self.text_before_cursor = text


def _completions(completer: SlashCompleter, text: str):
    return [c.text for c in completer.get_completions(_Doc(text), None)]


def _fake_choice(provider: str, model: str, display_name: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider, model=model, display_name=display_name or model
    )


@pytest.fixture
def fake_models(monkeypatch):
    """Patch available_models() to a deterministic, provider-diverse set."""
    choices = [
        _fake_choice("openai", "gpt-4o", "GPT-4o"),
        _fake_choice("openai", "gpt-4o-mini", "GPT-4o mini"),
        _fake_choice("anthropic", "claude-opus", "Claude Opus"),
        _fake_choice("openai", "gpt-4o", "dup"),  # duplicate provider+model
    ]
    monkeypatch.setattr(
        "modules.llm.available_models.available_models", lambda *a, **k: choices
    )
    return choices


# ---------------------------------------------------------------------------
# /model — provider then model
# ---------------------------------------------------------------------------


def test_model_completes_distinct_providers(fake_models):
    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/model ")
    # distinct providers only (openai appears 3x in the source)
    assert out.count("openai") == 1
    assert set(out) == {"openai", "anthropic"}


def test_model_provider_prefix_filters(fake_models):
    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/model open")
    assert out == ["openai"]


def test_model_completes_models_for_provider(fake_models):
    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/model openai ")
    assert set(out) == {"gpt-4o", "gpt-4o-mini"}  # deduped, provider-scoped
    # anthropic model must NOT leak into the openai list
    assert "claude-opus" not in out


def test_model_second_arg_prefix_filters(fake_models):
    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/model openai gpt-4o-m")
    assert out == ["gpt-4o-mini"]


def test_model_completion_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr("modules.llm.available_models.available_models", _boom)
    reg = build_default_registry()
    c = SlashCompleter(reg)
    assert _completions(c, "/model ") == []


# ---------------------------------------------------------------------------
# /toolset — named toolsets
# ---------------------------------------------------------------------------


def test_toolset_completes_names():
    from agents.task.tool_defaults import TOOLSETS

    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/toolset ")
    assert set(out) == set(TOOLSETS.keys())
    assert "default" in out


def test_toolset_prefix_filters():
    from agents.task.tool_defaults import TOOLSETS

    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/toolset def")
    assert out == [n for n in sorted(TOOLSETS) if n.startswith("def")]
    assert "default" in out


def test_toolset_no_completion_past_first_arg():
    reg = build_default_registry()
    c = SlashCompleter(reg)
    assert _completions(c, "/toolset default extra") == []


# ---------------------------------------------------------------------------
# /persona — persona names
# ---------------------------------------------------------------------------


def test_persona_completes_names(monkeypatch):
    monkeypatch.setattr(
        "cli.ui.commands.handlers._list_persona_names",
        lambda *a, **k: ["alice", "albert", "bob"],
    )
    reg = build_default_registry()
    c = SlashCompleter(reg)
    out = _completions(c, "/persona al")
    assert set(out) == {"alice", "albert"}


def test_persona_completion_fails_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no characters dir")

    monkeypatch.setattr("cli.ui.commands.handlers._list_persona_names", _boom)
    reg = build_default_registry()
    c = SlashCompleter(reg)
    assert _completions(c, "/persona ") == []


# ---------------------------------------------------------------------------
# Regression: existing behaviour is preserved
# ---------------------------------------------------------------------------


def test_command_name_completion_still_works():
    reg = build_default_registry()
    c = SlashCompleter(reg)
    assert "model" in _completions(c, "/mod")


def test_no_completion_without_slash():
    reg = build_default_registry()
    c = SlashCompleter(reg)
    assert _completions(c, "hello") == []


def test_replay_session_ids_still_complete():
    reg = build_default_registry()
    c = SlashCompleter(reg, sessions_provider=lambda: ["abc123", "abd999", "zzz"])
    # alias `/resume` must route to the `replay` command's completion
    assert set(_completions(c, "/resume ab")) == {"abc123", "abd999"}


def test_unknown_command_with_arg_yields_nothing():
    reg = CommandRegistry()
    reg.register(Command("noop", lambda ctx: None))
    c = SlashCompleter(reg)
    assert _completions(c, "/noop something") == []


def test_alias_completion_shows_pointer_not_duplicate_help():
    """/compress is an alias of /compact — its menu row must read `→ /compact`,
    not repeat /compact's help (which made them look like duplicate commands)."""
    from cli.ui.commands.handlers import default_registry
    from cli.ui.commands.registry import SlashCompleter

    completer = SlashCompleter(default_registry())
    comps = {c.text: c for c in completer.get_completions(_Doc("/comp"), None)}
    assert "compact" in comps and "compress" in comps
    assert comps["compress"].display_meta_text == "→ /compact"
    assert comps["compact"].display_meta_text != comps["compress"].display_meta_text
    assert "background" in comps["compact"].display_meta_text
