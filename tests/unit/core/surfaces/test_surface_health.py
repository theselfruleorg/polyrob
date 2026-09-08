"""030 WS-G3: the surface-health view — which surfaces are connected, and are
their delivery paths alive."""
from core.surfaces.envelopes import SurfaceCapabilities
from core.surfaces.health import render_surface_health, surface_health


class _Surface:
    def __init__(self, sid, media=False):
        self.surface_id = sid
        self.capabilities = SurfaceCapabilities(media_out=media)

    async def send(self, msg):
        return None


class _Registry:
    def __init__(self, surfaces):
        self._s = surfaces

    def all(self):
        return self._s


class _Router:
    def __init__(self, ids):
        self._surfaces = {i: object() for i in ids}


class _Breaker:
    def __init__(self, open_ids=()):
        self._open = set(open_ids)

    def is_open(self, sid):
        return sid in self._open


class _Container:
    def __init__(self, services):
        self._s = services

    def get_service(self, name):
        return self._s.get(name)


def test_registered_and_subscribed_surface_reads_healthy():
    c = _Container({
        "surface_registry": _Registry([_Surface("telegram", media=True)]),
        "message_router": _Router(["telegram"]),
        "surface_circuit_breaker": _Breaker(),
    })
    rows = surface_health(c)
    assert rows == [{
        "surface_id": "telegram", "registered": True, "subscribed": True,
        "media_out": True, "streaming": False, "max_message": 4096,
        "circuit": "closed", "dead_targets": 0,
    }]


def test_open_circuit_is_visible():
    c = _Container({
        "surface_registry": _Registry([_Surface("slack")]),
        "message_router": _Router(["slack"]),
        "surface_circuit_breaker": _Breaker(open_ids={"slack"}),
    })
    assert surface_health(c)[0]["circuit"] == "OPEN"


def test_subscribed_but_unregistered_is_called_out():
    c = _Container({
        "surface_registry": _Registry([]),
        "message_router": _Router(["discord"]),
    })
    rows = surface_health(c)
    assert rows[0]["surface_id"] == "discord"
    assert rows[0]["registered"] is False
    lines = render_surface_health(rows)
    assert any("UNREGISTERED" in ln for ln in lines)


def test_no_bus_renders_honestly():
    assert render_surface_health(surface_health(_Container({}))) == [
        "no surfaces registered (chat-surface bus not installed)"]
