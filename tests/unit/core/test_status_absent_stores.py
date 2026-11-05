"""A store that is made on first use is not a check that failed."""
import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from core import status_render
from core.status_render import _plain_absent, absent_while_idle, health_headline


def _snap(sources, overall="partial"):
    return SimpleNamespace(overall=overall, unavailable_sources=sources, health=[], sections={})


ABSENT = "work (FileNotFoundError: goals.db not found at /home/u/.polyrob/data/goals.db)"


def test_absent_stores_with_autonomy_off_read_as_ok(monkeypatch):
    monkeypatch.setattr("core.status_loops.autonomy_idle_reason", lambda: "autonomy is off")
    # A readable section beside the absent store: the case that read PARTIAL after a first chat.
    monkeypatch.setattr(status_render, "_is_new_home", lambda _s: False)
    snap = _snap([ABSENT])
    assert absent_while_idle(snap)
    assert health_headline(snap).startswith("Health: OK")
    assert "FileNotFoundError" not in health_headline(snap)


def test_absent_stores_with_autonomy_on_stay_partial(monkeypatch):
    monkeypatch.setattr("core.status_loops.autonomy_idle_reason", lambda: "")
    monkeypatch.setattr(status_render, "_is_new_home", lambda _s: False)
    assert health_headline(_snap([ABSENT])).startswith("Health: PARTIAL")


def test_an_unreadable_store_never_reads_as_absent(monkeypatch):
    monkeypatch.setattr("core.status_loops.autonomy_idle_reason", lambda: "autonomy is off")
    monkeypatch.setattr(status_render, "_is_new_home", lambda _s: False)
    snap = _snap([ABSENT, "delivery (DatabaseError: file is not a database)"])
    assert not absent_while_idle(snap)
    assert health_headline(snap).startswith("Health: PARTIAL")


def test_a_detail_line_names_an_absent_store_plainly():
    line = "cron: unavailable (FileNotFoundError: cron.db not found at /d/cron.db)"
    assert _plain_absent(line) == "cron: nothing yet (cron.db not created)"
    other = "cron: unavailable (DatabaseError: malformed)"
    assert _plain_absent(other) == other


def test_a_wallet_off_reconcile_is_not_a_fallen_gate(monkeypatch):
    from core.wallet.config import WALLET_OFF_TEXT
    from tools.defi.data_tool import DefiDataTool, ReconcileParams
    tool = DefiDataTool()
    recorded = []
    monkeypatch.setattr(tool, "_record_precondition_failed",
                        lambda *a: recorded.append(a))
    monkeypatch.setattr(tool, "_reconcile_impl_sync",
                        lambda *_a: SimpleNamespace(error=WALLET_OFF_TEXT))
    asyncio.run(tool.reconcile(ReconcileParams(chain="base")))
    assert recorded == []

    monkeypatch.setattr(tool, "_reconcile_impl_sync",
                        lambda *_a: SimpleNamespace(error="ledger over 1 MB"))
    asyncio.run(tool.reconcile(ReconcileParams(chain="base")))
    assert len(recorded) == 1


def test_the_book_of_an_install_with_no_wallet_reads_no_chain(monkeypatch, tmp_path):
    from tools.defi import book
    monkeypatch.setattr("tools.defi.defi_data_enabled", lambda: True)
    monkeypatch.setattr(book, "_wallet_is_on", lambda: False)
    monkeypatch.setattr("core.open_positions.positions_for", lambda *_a, **_k: [])
    out = asyncio.run(book.read_book("u1", str(tmp_path)))
    assert out["verdict"] == "no_wallet"
    assert out["chains"] == {} and out["error"] is None


def test_a_position_with_the_wallet_off_still_gets_a_real_read(monkeypatch, tmp_path):
    from tools.defi import book

    class _ReadStarted(Exception):
        pass

    def _tool():
        raise _ReadStarted

    monkeypatch.setattr("tools.defi.defi_data_enabled", lambda: True)
    monkeypatch.setattr(book, "_wallet_is_on", lambda: False)
    monkeypatch.setattr("core.open_positions.positions_for", lambda *_a, **_k: [object()])
    monkeypatch.setattr("tools.defi.data_tool.DefiDataTool", _tool)
    with pytest.raises(_ReadStarted):
        asyncio.run(book.read_book("u1", str(tmp_path)))


@pytest.mark.skipif(os.geteuid() == 0, reason="root owns everything")
def test_a_home_behind_a_symlink_can_use_its_own_overlay(tmp_path, monkeypatch):
    """/home -> /var/home on Fedora Atomic: the installed overlay was refused as untrusted."""
    from core import lazy_deps
    monkeypatch.setattr("core.lazy_installer.valid_feature", lambda _n: True)
    real = tmp_path.resolve() / "real"
    feature = real / "pylibs" / "digest" / "surface.telegram"
    (feature / "lib").mkdir(parents=True)
    (feature / ".complete").write_text("")
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    for path in (feature, feature / "lib", feature.parent, feature.parent.parent, real):
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
    (feature / ".complete").chmod(0o644)
    if not lazy_deps._protected_overlay(feature, local=True):
        pytest.skip("this machine's temp dir is shared, so the direct path is refused too")
    assert lazy_deps._protected_overlay(link / "pylibs" / "digest" / "surface.telegram", local=True)


def test_a_protected_overlay_still_refuses_any_symlink(tmp_path, monkeypatch):
    from core import lazy_deps
    monkeypatch.setattr("core.lazy_installer.valid_feature", lambda _n: True)
    monkeypatch.setattr("pwd.getpwnam", lambda _n: SimpleNamespace(pw_uid=os.geteuid()))
    real = tmp_path.resolve() / "real"
    feature = real / "f"
    feature.mkdir(parents=True)
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    assert lazy_deps._protected_overlay(link / "f", local=False) is False
