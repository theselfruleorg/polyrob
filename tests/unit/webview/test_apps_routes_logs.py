"""032 — the console's app-logs route validates the slug and resolves the tenant's
ROW before it touches the filesystem (it used to build a path out of the raw path
parameter first). Mirrors the agent tool's own ``logs()`` action."""
import asyncio
import types

import pytest
from fastapi import HTTPException


class _Pages:
    def __init__(self, data_dir):
        self._dir = data_dir

    def _effective_user_id(self, request):
        return "owner-1"

    def _data_dir(self):
        return self._dir


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from core.app_service.registry import AppServiceRegistry
    import webview.apps_routes as mod
    reg = AppServiceRegistry(str(tmp_path / "a.db"))
    reg.upsert_request("rob-status", "owner-1", source_dir="d", cmd=["x"], container_port=80,
                       health_path="/", egress="none", egress_allow=[], env={},
                       workspace_digest="d" * 64)
    reads = []

    def _tail(data_dir, user_id, slug, n=50):
        reads.append((data_dir, user_id, slug, n))
        return "log line\n"

    monkeypatch.setattr(mod, "_p", lambda: _Pages(str(tmp_path / "data")))
    monkeypatch.setattr(mod, "_registry", lambda: reg)
    monkeypatch.setattr("core.app_service.owner_ops.logs_tail", _tail)
    return types.SimpleNamespace(mod=mod, reg=reg, reads=reads)


def _logs(rig, slug, n=100):
    return asyncio.run(rig.mod.api_apps_logs(None, slug, n))


@pytest.mark.parametrize("slug", ["../../../etc", "..", "rob status", "Rob-Status",
                                  "rob-status/../../x", "a" * 200, ""])
def test_invalid_slug_is_refused_before_any_filesystem_read(rig, slug):
    with pytest.raises(HTTPException) as e:
        _logs(rig, slug)
    assert e.value.status_code == 400
    assert rig.reads == []


def test_unknown_slug_for_this_tenant_is_404_not_a_path_read(rig):
    with pytest.raises(HTTPException) as e:
        _logs(rig, "someone-elses-app")
    assert e.value.status_code == 404
    assert rig.reads == []


def test_the_tenants_own_app_still_reads(rig):
    res = _logs(rig, "rob-status", 5)
    assert res.status_code == 200
    assert rig.reads == [(rig.mod._p()._data_dir(), "owner-1", "rob-status", 5)]
