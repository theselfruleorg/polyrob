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
    assert "not set up" in out.lower() or "no avatar" in out.lower()
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
    assert "already" in out.lower()
    assert "randomize" in out.lower() and "keep" in out.lower()   # the setup verbs


def test_registered_in_default_registry():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("pfp") is not None
    assert reg.lookup("avatar") is not None   # alias


def test_generate_reports_identity_and_next_steps(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    ctx = _make_ctx(["generate"])
    h_pfp(ctx)
    out = "\n".join(ctx._emitted)
    assert "voice" in out.lower()          # the voice signature is surfaced, not skipped
    assert "next:" in out.lower()          # ...and the user is told what to do with it


def test_randomize_rerolls_the_stored_avatar(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))
    from core.instance import load_pfp_meta
    first = load_pfp_meta(tmp_path, "rob")
    ctx = _make_ctx(["randomize"])
    h_pfp(ctx)
    second = load_pfp_meta(tmp_path, "rob")
    assert second["variant"] != first["variant"]   # a genuinely new identity
    out = "\n".join(ctx._emitted)
    assert "voice" in out.lower()


def test_randomize_voice_only(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))
    from core.instance import load_pfp_meta
    first = load_pfp_meta(tmp_path, "rob")
    ctx = _make_ctx(["randomize", "voice"])
    h_pfp(ctx)
    second = load_pfp_meta(tmp_path, "rob")
    assert second["variant"] == first["variant"]   # face untouched
    assert "voice" in second["override"]           # voice pinned to a fresh roll


def test_keep_locks_and_randomize_then_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))                      # draft
    ctx = _make_ctx(["keep"])
    h_pfp(ctx)
    assert "kept" in "\n".join(ctx._emitted).lower()
    from core.instance import load_pfp_meta
    kept = load_pfp_meta(tmp_path, "rob")
    assert kept["locked"] is True

    ctx2 = _make_ctx(["randomize"])                     # setup is over
    h_pfp(ctx2)
    out = "\n".join(ctx2._emitted).lower()
    assert "kept" in out or "once" in out               # refused, explained
    assert load_pfp_meta(tmp_path, "rob")["variant"] == kept["variant"]   # unchanged


def test_generate_refuses_after_keep(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))
    h_pfp(_make_ctx(["keep"]))
    from core.instance import load_pfp_meta
    before = load_pfp_meta(tmp_path, "rob")
    ctx = _make_ctx(["generate", "force"])              # even force can't change identity
    h_pfp(ctx)
    after = load_pfp_meta(tmp_path, "rob")
    assert after["variant"] == before["variant"]


def test_say_speaks_and_reports_the_voice(monkeypatch, tmp_path):
    import modules.pfp.voice as voicemod
    monkeypatch.setattr("cli.commands.pfp._instance_home", lambda: (tmp_path, "rob"))
    h_pfp(_make_ctx(["generate"]))                        # draft with a real voice
    spoken = {}
    monkeypatch.setattr(voicemod, "speak_voice",
                        lambda voice, text, **kw: spoken.setdefault("voice", voice) and "say" or "say")
    ctx = _make_ctx(["say"])
    h_pfp(ctx)
    out = "\n".join(ctx._emitted)
    assert "voice" in out.lower() and "spoken via" in out
    assert set(spoken["voice"]) == {"pitch", "rate", "timbre"}
