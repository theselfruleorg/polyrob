"""098 AG-07: the taint sidecar commits under the taint lock, atomically."""
import threading

from agents.task.session.hitl_ingress import HITLIngressMixin


class _Orch(HITLIngressMixin):
    def __init__(self, path):
        self._path = path
        self.writes = 0

    def _taint_sidecar_path(self):
        return self._path

    def _write_taint_sidecar(self, sources):
        self.writes += 1
        super()._write_taint_sidecar(sources)


def test_sidecar_matches_memory_under_threads(tmp_path):
    o = _Orch(tmp_path / "correspondent_taint.json")

    def _set():
        for _ in range(300):
            o._set_correspondent_taint("email", "a@b.c")

    def _clear():
        for _ in range(300):
            o._clear_correspondent_taint()
    ts = [threading.Thread(target=_set), threading.Thread(target=_clear)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert o._path.exists() == bool(o._correspondent_tainted)


def test_clear_when_untainted_does_not_touch_disk(tmp_path):
    o = _Orch(tmp_path / "correspondent_taint.json")
    o._clear_correspondent_taint()
    assert o.writes == 0
    o._set_correspondent_taint("telegram", "42")
    o._clear_correspondent_taint()
    assert o.writes == 2 and not o._path.exists()
