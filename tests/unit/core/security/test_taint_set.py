"""W5 (D3): the correspondent taint behind ONE view (``core.security.taints``)."""
import pathlib
from types import SimpleNamespace

from core.security import taints

REPO = pathlib.Path(__file__).resolve().parents[4]


class _Orch:
    def __init__(self):
        self._correspondent_tainted = False

    def _clear_correspondent_taint(self):
        self._correspondent_tainted = False


def test_the_one_clear_point_clears_the_correspondent_taint():
    orch = _Orch()
    orch._correspondent_tainted = True
    assert taints.correspondent_tainted(orch)
    taints.clear_on_owner_turn(orch)
    assert not taints.correspondent_tainted(orch)


def test_turn_tainted_is_the_correspondent_taint():
    assert taints.turn_tainted(SimpleNamespace(metadata={"correspondent_tainted": True}))
    assert not taints.turn_tainted(SimpleNamespace(metadata={}))
    assert not taints.turn_tainted(SimpleNamespace())


def test_probe_reads_at_call_time():
    orch = _Orch()
    probe = taints.correspondent_probe(orch)
    assert probe() is False
    orch._correspondent_tainted = True
    assert probe() is True


def test_user_ingress_clears_through_the_one_point():
    text = (REPO / "agents/task/agent/core/user_ingress.py").read_text()
    assert "clear_on_owner_turn(orchestrator)" in text


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
