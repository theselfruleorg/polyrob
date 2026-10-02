"""CLI6 (2026-10-03 audit): the REPL `/journey` and `/recap` read the deployed
home and default to the SAME 24h window Telegram's `/recap` uses.

They read ``config.data_dir`` (the shell's home, not the daemon's) and defaulted
to 7d, so one verb answered a different question on each seat.
"""
import types

from cli.ui.commands import h_journey


def _run(monkeypatch, args):
    seen = {}

    def fake_render(*, user_id, since_label, data_dir):
        seen.update(user_id=user_id, since_label=since_label, data_dir=data_dir)
        return "ok"

    monkeypatch.setattr(h_journey, "render_journey", fake_render)
    monkeypatch.setattr("cli._admin_home.admin_data_dir", lambda write=None: "/deployed/home")
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid=None: "tg_owner")
    cfg = types.SimpleNamespace(data_dir="/shell/home")
    ctx = types.SimpleNamespace(args=args, user_id="local",
                                container=types.SimpleNamespace(config=cfg),
                                emit=lambda *a, **k: None)
    h_journey.h_journey(ctx)
    return seen


def test_default_window_is_24h(monkeypatch):
    assert _run(monkeypatch, [])["since_label"] == "24h"


def test_explicit_window_is_kept(monkeypatch):
    assert _run(monkeypatch, ["7d"])["since_label"] == "7d"


def test_reads_the_deployed_home_and_owner(monkeypatch):
    seen = _run(monkeypatch, [])
    assert seen["data_dir"] == "/deployed/home"
    assert seen["user_id"] == "tg_owner"


def test_renderer_default_is_24h():
    """`polyrob journey` (and this renderer) defaulted to 7d while Telegram and the
    REPL default to 24h; the click default is pinned in commands/test_journey_cli.py."""
    import inspect
    from cli.ui.commands.h_journey import render_journey as real
    assert inspect.signature(real).parameters["since_label"].default == "24h"
