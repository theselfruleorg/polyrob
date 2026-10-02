"""/avatar in the REPL (cli/ui/commands/h_avatar.py) — show and set the slot."""
from __future__ import annotations

import base64

from cli.ui.commands.h_avatar import h_avatar
from cli.ui.commands.registry import CommandContext

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _ctx(args):
    ctx = CommandContext(args=list(args))
    ctx._emitted = []
    ctx.emit = lambda text, **kw: ctx._emitted.append(text)  # type: ignore[method-assign]
    return ctx


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.avatar._home",
                        lambda write=None: (str(tmp_path), "rob"))


def test_show_reports_the_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    ctx = _ctx([])
    h_avatar(ctx)
    out = "\n".join(ctx._emitted)
    assert "avatar the default" in out and "/avatar set" in out


def test_show_reports_not_set_without_the_shipped_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr("core.avatar.DEFAULT_AVATAR", tmp_path / "missing.png")
    ctx = _ctx([])
    h_avatar(ctx)
    out = "\n".join(ctx._emitted)
    assert "avatar not set" in out and "/avatar set" in out


def test_set_from_a_file_then_show(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    f = tmp_path / "me.png"
    f.write_bytes(PNG)
    h_avatar(_ctx(["set", str(f)]))
    ctx = _ctx(["show"])
    h_avatar(ctx)
    assert "set (file:me.png)" in "\n".join(ctx._emitted)


def test_set_from_a_data_url(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    ctx = _ctx(["set", "data:image/png;base64," + base64.b64encode(PNG).decode()])
    h_avatar(ctx)
    assert "avatar set (url:data:" in "\n".join(ctx._emitted)


def test_a_non_image_is_refused(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    ctx = _ctx(["set", str(f)])
    h_avatar(ctx)
    assert "avatar not set" in "\n".join(ctx._emitted)


def test_registered_in_default_registry():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("avatar") is not None
    assert reg.lookup("pfp") is None
