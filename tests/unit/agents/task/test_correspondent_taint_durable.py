"""M02 (2026-09-23 security analysis): the correspondent taint survives an
eviction/restart. It used to live only in memory, while the correspondent's
message was restored with the history — a recreated session ran ungated."""
import pytest

from agents.task.session.hitl_ingress import HITLIngressMixin


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Orch(HITLIngressMixin):
    def __init__(self, sid="sess-m02", uid="u-m02"):
        self.agents = {}
        self.session_id = sid
        self.user_id = uid
        self.logger = _Logger()


@pytest.fixture(autouse=True)
def _pm(tmp_path):
    from agents.task.path import get_path_manager, set_path_manager
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    yield


def test_taint_is_written_and_restored_on_a_fresh_orchestrator():
    a = _Orch()
    a._set_correspondent_taint("email", "Mallory@Evil.com ")
    assert a._taint_sidecar_path().exists()
    b = _Orch()                                   # the recreated orchestrator
    assert not getattr(b, "_correspondent_tainted", False)
    assert b.restore_correspondent_taint() is True
    assert b._correspondent_tainted is True
    assert ("email", "mallory@evil.com") in b._correspondent_taint_sources


def test_owner_turn_clear_removes_the_record():
    a = _Orch()
    a._set_correspondent_taint("email", "x@y.z")
    a._clear_correspondent_taint()
    assert not a._taint_sidecar_path().exists()
    b = _Orch()
    assert b.restore_correspondent_taint() is False
    assert not getattr(b, "_correspondent_tainted", False)


def test_unreadable_record_taints_fail_closed():
    a = _Orch()
    path = a._taint_sidecar_path()
    path.write_text("{not json")
    assert a.restore_correspondent_taint() is True
    assert a._correspondent_tainted is True and a._correspondent_taint_sources == set()


def test_other_session_is_not_tainted():
    _Orch(sid="sess-a")._set_correspondent_taint("x", "1")
    assert _Orch(sid="sess-b").restore_correspondent_taint() is False


def test_recreate_path_restores_the_taint():
    import inspect
    import agents.task.task_agent_delivery as delivery
    src = inspect.getsource(delivery)
    assert "orchestrator.restore_correspondent_taint()" in src
