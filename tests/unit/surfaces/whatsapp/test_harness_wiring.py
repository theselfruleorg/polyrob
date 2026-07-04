# tests/unit/surfaces/whatsapp/test_harness_wiring.py
from surfaces.whatsapp.harness import build_whatsapp_harness


class _Router:
    def __init__(self): self.subs = {}
    def subscribe(self, sid, s): self.subs[sid] = s


class _Container:
    def __init__(self): self._svc = {"message_router": _Router()}
    def get_service(self, k): return self._svc.get(k)
    def register_service(self, k, v): self._svc[k] = v


def test_build_registers_surface_and_webhook(tmp_path):
    c = _Container()
    h = build_whatsapp_harness(c, task_agent=object(), data_dir=str(tmp_path))
    assert "whatsapp" in c.get_service("message_router").subs
    assert "whatsapp" in c.get_service("webhook_surfaces")
    assert c.get_service("whatsapp_sink") is not None


def test_whatsapp_is_not_a_local_owner_surface():
    from core.surfaces.access import _LOCAL_OWNER_SURFACES
    assert "whatsapp" not in _LOCAL_OWNER_SURFACES   # forgeable phone never auto-owns
