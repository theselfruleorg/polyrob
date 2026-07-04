"""Pure string/format tests for cli.ui.statusbar from synthetic SessionState."""

from __future__ import annotations

from cli.ui import statusbar
from cli.ui.state import SessionState


def _state(**kw) -> SessionState:
    s = SessionState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_status_text_has_model_tokens_cost():
    s = _state(model="gemini-2.5-flash", tokens_in=4991, tokens_out=159,
               cost_estimate_total=0.000422, status="running")
    out = statusbar.status_text(s)
    assert "gemini-2.5-flash" in out
    assert "5.0k" in out  # 4991 → 5.0k
    assert "159" in out
    assert "$0.0004" in out
    assert "running" in out


def test_status_text_includes_ctx_when_present():
    s = _state(model="m", ctx_percent=42.0)
    out = statusbar.status_text(s)
    assert "ctx 42%" in out


def test_status_text_omits_ctx_when_zero():
    s = _state(model="m", ctx_percent=0.0)
    out = statusbar.status_text(s)
    assert "ctx" not in out


def test_status_text_dash_model_when_empty():
    s = _state(model="", status="starting")
    out = statusbar.status_text(s)
    assert "—" in out


def test_status_text_spinner_prefixes_status():
    s = _state(model="m", status="running")
    out = statusbar.status_text(s, spinner="⠋ ")
    assert "⠋ running" in out


def test_fmt_elapsed_formats():
    assert statusbar._fmt_elapsed(5) == "5s"
    assert statusbar._fmt_elapsed(65) == "1m05s"
    assert statusbar._fmt_elapsed(3700) == "1h01m"


def test_status_formatted_returns_formatted_text():
    s = _state(model="gemini", tokens_in=10, tokens_out=2,
               cost_estimate_total=0.01, status="running")
    ft = statusbar.status_formatted(s, spinner="⠙ ")
    # FormattedText is a list of (style, text) tuples; flatten the text.
    text = "".join(frag[1] for frag in ft)
    assert "gemini" in text
    assert "$0.0100" in text
    assert "running" in text


def test_status_formatted_colors_error_status():
    s = _state(model="m", status="error")
    ft = statusbar.status_formatted(s)
    classes = [frag[0] for frag in ft]
    assert any("status.error" in c for c in classes)
