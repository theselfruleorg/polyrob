"""Phase 5: first-run banner content + secret-safety tests."""
from io import StringIO

from rich.console import Console

from cli.ui.banner import (
    banner_plain,
    print_banner,
    provider_key_status,
)
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


class _Config:
    """Stand-in for AgentConfig exposing the *_api_key attributes."""

    def __init__(self, **keys):
        self.openai_api_key = keys.get("openai")
        self.anthropic_api_key = keys.get("anthropic")
        self.gemini_api_key = keys.get("gemini")
        self.deepseek_api_key = keys.get("deepseek")
        self.openrouter_api_key = keys.get("openrouter")
        self.nvidia_api_key = keys.get("nvidia")


def test_provider_key_status_names_only():
    cfg = _Config(openai="sk-SECRET", anthropic="ak-SECRET")
    ready = provider_key_status(cfg)
    # Canonical PROFILES order (anthropic before openai) — one SSOT order now.
    assert ready == ["anthropic", "openai"]
    # Never returns the secret values.
    assert "sk-SECRET" not in ready and "ak-SECRET" not in ready


def test_provider_key_status_empty():
    assert provider_key_status(_Config()) == []


def test_banner_plain_content():
    text = banner_plain(
        version="1.0.0",
        model="gpt-5",
        provider="openai",
        tool_ids=["browser", "task"],
        session_id="abcdef1234567",
        providers_with_keys=["openai"],
    )
    assert "v1.0.0" in text
    assert "gpt-5 (openai)" in text
    assert "browser, task" in text
    assert "abcdef12" in text  # short session id
    assert "abcdef1234567" not in text  # full id is truncated
    # Two quiet lines — the banner must not compete with the dialog.
    assert len(text.splitlines()) == 2
    assert "keys" not in text  # key status moved out of the banner


def test_banner_plain_help_hint():
    text = banner_plain(
        version="1.0.0", model="m", provider="p", tool_ids=[],
        session_id="s", show_help_hint=True,
    )
    assert "/help" in text


def test_print_banner_rich_no_secret_values():
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=100)
    renderer = RichRenderer(state=SessionState(), console=console)
    cfg = _Config(openai="sk-SECRET-LEAK", gemini="gm-SECRET-LEAK")
    print_banner(
        renderer,
        version="1.0.0",
        model="gemini-2.5",
        provider="gemini",
        tool_ids=["filesystem"],
        session_id="deadbeefcafe",
        config=cfg,
    )
    out = buf.getvalue()
    assert "SECRET" not in out
    assert "v1.0.0" in out
    assert "gemini-2.5" in out
    assert "filesystem" in out
    assert "deadbeef" in out


def test_print_banner_plain_renderer_path():
    buf = StringIO()
    renderer = PlainRenderer(state=SessionState(), stream=buf)
    cfg = _Config(anthropic="ak-SECRET")
    print_banner(
        renderer,
        version="2.3.4",
        model="claude",
        provider="anthropic",
        tool_ids=["task"],
        session_id="0011223344",
        config=cfg,
    )
    out = buf.getvalue()
    assert "SECRET" not in out
    assert "v2.3.4" in out
    assert "claude (anthropic)" in out
