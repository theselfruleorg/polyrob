"""`polyrob avatar` (cli/commands/avatar.py) — the avatar slot from the shell."""
from __future__ import annotations

from click.testing import CliRunner

from cli.commands.avatar import avatar
from core import avatar as slot

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.commands.avatar._home",
                        lambda write=None: (str(tmp_path), "rob"))


def test_show_with_no_avatar_shows_the_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    res = CliRunner().invoke(avatar, ["show"])
    assert res.exit_code == 0, res.output
    assert "rob — avatar the default" in res.output
    assert "polyrob avatar set" in res.output
    assert str(slot.DEFAULT_AVATAR) in res.output


def test_show_without_the_shipped_default_says_not_set(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(slot, "DEFAULT_AVATAR", tmp_path / "missing.png")
    res = CliRunner().invoke(avatar, ["show"])
    assert res.exit_code == 0 and "rob — avatar not set" in res.output
    assert "polyrob avatar set" in res.output


def test_bare_group_shows(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    res = CliRunner().invoke(avatar, [])
    assert res.exit_code == 0 and "avatar the default" in res.output


def test_set_from_a_file(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    f = tmp_path / "face.png"
    f.write_bytes(PNG)
    res = CliRunner().invoke(avatar, ["set", str(f)])
    assert res.exit_code == 0, res.output
    assert "set (file:face.png)" in res.output
    assert slot.load_avatar(tmp_path, "rob").is_set


def test_set_from_a_url(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    seen = {}

    async def fake_fetch(url, **kw):
        seen["url"] = url
        return PNG

    monkeypatch.setattr("tools.avatar_sources.fetch_bytes", fake_fetch)
    res = CliRunner().invoke(avatar, ["set", "https://example.com/face.png"])
    assert res.exit_code == 0, res.output
    assert seen["url"] == "https://example.com/face.png"
    assert slot.load_avatar(tmp_path, "rob").source == "url:https://example.com/face.png"


def test_set_refuses_a_non_image(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    res = CliRunner().invoke(avatar, ["set", str(f)])
    assert res.exit_code != 0
    assert "not a PNG" in res.output


def test_set_needs_exactly_one_source(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert CliRunner().invoke(avatar, ["set"]).exit_code != 0


def test_clear(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    slot.set_avatar(tmp_path, "rob", PNG, source="x")
    res = CliRunner().invoke(avatar, ["clear", "--yes"])
    assert res.exit_code == 0 and "removed" in res.output
    assert slot.load_avatar(tmp_path, "rob").is_default


def test_push_refuses_with_no_avatar(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(slot, "DEFAULT_AVATAR", tmp_path / "missing.png")
    res = CliRunner().invoke(avatar, ["push"])
    assert res.exit_code != 0 and "no avatar to push" in res.output


def test_push_works_with_the_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("PFP_PUSH_TWITTER", "true")
    calls = []
    monkeypatch.setattr("modules.avatar.push.push_twitter", lambda p: calls.append(p))
    res = CliRunner().invoke(avatar, ["push", "--twitter"])
    assert res.exit_code == 0 and "updated" in res.output, res.output
    assert calls == [slot.DEFAULT_AVATAR]


def test_push_refuses_an_svg(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    slot.set_avatar(tmp_path, "rob", SVG, source="nft:base:0x1:1")
    res = CliRunner().invoke(avatar, ["push", "--twitter"])
    assert res.exit_code != 0 and "SVG" in res.output


def test_push_is_flag_gated(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("PFP_PUSH_TWITTER", raising=False)
    slot.set_avatar(tmp_path, "rob", PNG, source="x")
    res = CliRunner().invoke(avatar, ["push", "--twitter"])
    assert res.exit_code == 0 and "twitter: disabled" in res.output


def test_push_twitter_is_hash_idempotent(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("PFP_PUSH_TWITTER", "true")
    slot.set_avatar(tmp_path, "rob", PNG, source="x")
    calls = []
    monkeypatch.setattr("modules.avatar.push.push_twitter", lambda p: calls.append(p))
    r1 = CliRunner().invoke(avatar, ["push", "--twitter"])
    r2 = CliRunner().invoke(avatar, ["push", "--twitter"])
    assert "updated" in r1.output and "unchanged, skipped" in r2.output
    assert len(calls) == 1
