"""Tests for the ``/pfp`` slash-command handler (cli/ui/commands/h_pfp.py).

Avatar generation stays OPTIONAL (owner decision 2026-07-14) — nothing auto-creates
it; `/pfp generate` is the discoverable, explicit way to do it from inside the REPL.
These tests exercise status/generate/idempotence plus the default-registry wiring
(``pfp`` + alias ``avatar``).
"""
from __future__ import annotations

from cli.ui.commands.registry import CommandContext
from cli.ui.commands.h_pfp import h_pfp


def _make_ctx(args):
    """A CommandContext with ``emit`` overridden to capture output (no renderer needed)."""
    ctx = CommandContext(args=list(args))
    ctx._emitted = []
    ctx.emit = lambda text, **kw: ctx._emitted.append(text)  # type: ignore[method-assign]
    return ctx


def test_status_reports_no_avatar(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    ctx = _make_ctx([])
    h_pfp(ctx)
    out = "\n".join(ctx._emitted)
    assert "not generated" in out.lower() or "no avatar" in out.lower()
    assert "/pfp generate" in out


def test_generate_creates_avatar(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    ctx = _make_ctx(["generate"])
    h_pfp(ctx)
    from core.instance import pfp_path
    assert pfp_path(tmp_path, "rob").is_file()   # committed-reference fallback OK
    out = "\n".join(ctx._emitted)
    assert "generated" in out.lower()


def test_generate_is_idempotent_without_force(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))
    ctx = _make_ctx(["generate"])
    h_pfp(ctx)
    out = "\n".join(ctx._emitted)
    assert "already" in out.lower() and "force" in out.lower()


def test_registered_in_default_registry():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("pfp") is not None
    assert reg.lookup("avatar") is not None   # alias
