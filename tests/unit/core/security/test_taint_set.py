"""W5 (D3): the three taints behind ONE view (``core.security.taints``)."""
import pathlib
from types import SimpleNamespace

import pytest

from core.security import refusal_taint, taints

REPO = pathlib.Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.delenv("SECURITY_SETTINGS", raising=False)
    store = {}
    from core.security import refusal_taint_store
    monkeypatch.setattr(refusal_taint_store, "read", lambda k: store.get(k, False))
    monkeypatch.setattr(refusal_taint_store, "write",
                        lambda k, tainted: store.__setitem__(k, tainted))
    refusal_taint.reset_for_tests()
    yield
    refusal_taint.reset_for_tests()


class _Orch:
    session_id = "sid-1"

    def __init__(self):
        self._correspondent_tainted = False

    def _clear_correspondent_taint(self):
        self._correspondent_tainted = False


def test_empty():
    assert not taints.of(orchestrator=_Orch())
    assert taints.of() == taints.EMPTY


def test_each_taint_is_seen():
    orch = _Orch()
    orch._correspondent_tainted = True
    from core.security import read_taint
    read_taint.mark(orch)
    refusal_taint.mark("sid-1", action="defi_trade_swap")
    held = taints.of(orchestrator=orch)
    assert held.correspondent and held.read and held.refusal
    assert set(held.names) == set(taints.ALL)


def test_one_clear_point_clears_all_three():
    orch = _Orch()
    orch._correspondent_tainted = True
    from core.security import read_taint
    read_taint.mark(orch)
    refusal_taint.mark("sid-1", action="x")
    taints.clear_on_owner_turn(orch)
    assert not taints.of(orchestrator=orch)


def test_turn_tainted_keeps_the_correspondent_taint_when_read_taint_is_off(monkeypatch):
    """``read_taint=off`` is the owner's row for the READ taint only; a
    correspondent's message in the session still taints the turn."""
    monkeypatch.setenv("SECURITY_SETTINGS", "read_taint=off")
    ctx = SimpleNamespace(metadata={"untrusted_read": True, "correspondent_tainted": True})
    assert taints.turn_tainted(ctx) is True
    ctx = SimpleNamespace(metadata={"untrusted_read": True})
    assert taints.turn_tainted(ctx) is False
    monkeypatch.delenv("SECURITY_SETTINGS")
    assert taints.turn_tainted(ctx) is True


def test_probe_reads_at_call_time():
    orch = _Orch()
    probe = taints.correspondent_probe(orch)
    assert probe() is False
    orch._correspondent_tainted = True
    assert probe() is True


def test_user_ingress_clears_through_the_one_point():
    text = (REPO / "agents/task/agent/core/user_ingress.py").read_text()
    assert "clear_on_owner_turn(orchestrator)" in text
    assert "_clear_refusal_taint" not in text and "_clear_read_taint" not in text


#: Files that still read the correspondent attribute directly. SHRINK-ONLY:
#: a reader goes through ``core.security.taints``.
_RAW_READERS = {
    "core/security/taints.py",
}


def test_correspondent_attribute_is_read_through_the_taint_view():
    import re
    pat = re.compile(r"getattr\([^)]*[\"']_correspondent_tainted[\"']")
    found = set()
    for tree in ("agents", "api", "cli", "core", "cron", "modules", "packs", "surfaces",
                 "tools", "webview"):
        for p in (REPO / tree).rglob("*.py"):
            rel = p.relative_to(REPO).as_posix()
            if "/tests/" in f"/{rel}":
                continue
            if pat.search(p.read_text(errors="ignore")):
                found.add(rel)
    assert found <= _RAW_READERS, sorted(found - _RAW_READERS)
