from core.surfaces.registry import SurfaceRegistry, register_surface, is_surface_enabled
from core.surfaces.envelopes import SurfaceCapabilities, SendResult
from core.surfaces.surface import Surface


class _FakeContainer:
    def __init__(self):
        self._svc = {}

    def get_service(self, n):
        return self._svc.get(n)

    def register_service(self, n, inst, **k):
        self._svc[n] = inst


class _S(Surface):
    def __init__(self, sid):
        super().__init__()
        self._sid = sid

    @property
    def surface_id(self):
        return self._sid

    @property
    def capabilities(self):
        return SurfaceCapabilities()

    async def send(self, msg):
        return SendResult(success=True)

    async def start(self, c):
        pass

    async def stop(self):
        pass


def test_register_adds_and_is_enabled():
    c = _FakeContainer()
    register_surface(c, _S("cli"))
    reg = c.get_service("surface_registry")
    assert reg.get("cli") is not None
    assert is_surface_enabled(c, "cli") is True
    assert is_surface_enabled(c, "telegram") is False


def test_register_subscribes_to_router_if_present():
    c = _FakeContainer()

    class _Router:
        def __init__(self):
            self.subs = {}

        def subscribe(self, sid, s):
            self.subs[sid] = s

    c.register_service("message_router", _Router())
    register_surface(c, _S("telegram"))
    assert "telegram" in c.get_service("message_router").subs
