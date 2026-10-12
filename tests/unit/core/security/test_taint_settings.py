"""D1 (2026-10-10): the two taints can refuse natural work, so each reads its
owner-set SECURITY_SETTINGS row. Defaults keep today's behaviour (on / hold);
``off`` makes the taint never set and never read."""
import types

import pytest

from core.security import read_taint, refusal_taint
from core.security import settings as ss


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv(ss.ENV, raising=False)
    refusal_taint.reset_for_tests()
    yield
    refusal_taint.reset_for_tests()


def _ctx():
    return types.SimpleNamespace(metadata={})


def test_read_taint_is_on_by_default():
    orch, ctx = types.SimpleNamespace(), _ctx()
    read_taint.mark(orch, ctx)
    assert read_taint.is_tainted(ctx) and read_taint.orchestrator_tainted(orch)


def test_read_taint_off_never_sets_or_reads(monkeypatch):
    monkeypatch.setenv(ss.ENV, "read_taint=off")
    orch, ctx = types.SimpleNamespace(), _ctx()
    read_taint.mark(orch, ctx)
    assert not ctx.metadata.get(read_taint.META_KEY)       # raw readers see nothing
    assert not read_taint.is_tainted(ctx)
    assert not read_taint.orchestrator_tainted(orch)
    # a taint set earlier (before the owner turned it off) no longer reads
    ctx2 = types.SimpleNamespace(metadata={read_taint.META_KEY: True})
    assert not read_taint.is_tainted(ctx2)


def test_refusal_taint_holds_by_default(monkeypatch):
    store = {}
    monkeypatch.setattr("core.security.refusal_taint_store.read", lambda k: store.get(k, False))
    monkeypatch.setattr("core.security.refusal_taint_store.write",
                        lambda k, tainted: store.__setitem__(k, tainted))
    refusal_taint.mark("run1", action="defi_trade_swap")
    assert refusal_taint.is_tainted("run1")


def test_refusal_taint_off_never_holds(monkeypatch):
    store = {}
    monkeypatch.setattr("core.security.refusal_taint_store.read", lambda k: store.get(k, False))
    monkeypatch.setattr("core.security.refusal_taint_store.write",
                        lambda k, tainted: store.__setitem__(k, tainted))
    monkeypatch.setenv(ss.ENV, "refusal_taint=off")
    refusal_taint.mark("run1", action="defi_trade_swap")
    assert store == {} and not refusal_taint.is_tainted("run1")
    store["run2"] = True                                   # persisted before the change
    assert not refusal_taint.is_tainted("run2")
