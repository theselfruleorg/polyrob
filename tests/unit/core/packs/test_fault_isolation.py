"""A malformed installed pack must not stop unrelated packs from loading."""
from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _init, _install, _toml


def test_bad_manifest_type_refuses_only_that_pack(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "bad", _toml("bad").replace(
        "capabilities = []", "capabilities = []\ncli = []"), _init("bad"))
    write_pack(tmp_path, monkeypatch, "good", _toml("good"), _init("good"))
    _install(scratch, monkeypatch, ["bad", "good"])
    scratch.load_packs()
    assert state.record("bad").status == state.REFUSED
    assert "TypeError" in state.record("bad").reason
    assert state.record("good").status == state.LOADED
    assert not state.discovery_error()


def test_failing_cli_import_refuses_only_that_pack(scratch, tmp_path, monkeypatch):
    pkg = write_pack(tmp_path, monkeypatch, "bad", _toml("bad") +
                     '\n[cli]\ncommands = ["bad"]\n',
                     _init("bad", extra=", cli=('polyrob_bad.cmd:bad',)"))
    (pkg / "cmd.py").write_text("raise RuntimeError('broken command dependency')\n")
    write_pack(tmp_path, monkeypatch, "good", _toml("good"), _init("good"))
    _install(scratch, monkeypatch, ["bad", "good"])
    scratch.load_packs()
    assert state.record("bad").status == state.REFUSED
    assert "broken command dependency" in state.record("bad").reason
    assert state.record("good").status == state.LOADED
    assert not state.discovery_error()


def test_refused_pack_cannot_leave_auth_or_delivery_hooks_live(scratch, tmp_path, monkeypatch):
    from core import boot_reconcilers, delivery_channels, token_check_hook
    previous = lambda *args: None
    monkeypatch.setattr(token_check_hook, "_checker", previous)
    monkeypatch.setattr(boot_reconcilers, "_RECONCILERS", {})
    toml = _toml("bad").replace('capabilities = ["tools"]',
                               'capabilities = ["tools"]\ndelivery_channels = ["badpost"]')
    init = _init("bad", extra=", hooks={"
                 "'identity.token_checker': print, "
                 "'cron.delivery_channel': {'badpost': print}, "
                 "'autonomy.boot_reconciler': {'badt': print, 'foreign': print}}")
    write_pack(tmp_path, monkeypatch, "bad", toml, init)
    _install(scratch, monkeypatch, ["bad"])
    scratch.load_packs()
    assert state.record("bad").status == state.REFUSED
    assert token_check_hook.token_checker() is previous
    assert boot_reconcilers.boot_reconciler("badt") is None
    assert delivery_channels.sender_for("badpost") is None
