from core.surfaces.send_policy import SendDecision
from core.surfaces.envelopes import SurfaceCapabilities, OutboundMessage, SendResult
from core.surfaces.surface import Surface


class _FakeSurface(Surface):
    @property
    def surface_id(self): return "fake"
    @property
    def capabilities(self): return SurfaceCapabilities()
    async def send(self, msg): return SendResult(success=True)
    async def start(self, container): ...
    async def stop(self): ...


def test_base_can_send_now_defaults_to_allow():
    s = _FakeSurface()
    assert s.can_send_now("agent:main:fake:dm:1") == SendDecision.ALLOW


def test_capabilities_have_window_fields_default_off():
    caps = SurfaceCapabilities()
    assert caps.service_window_secs == 0
    assert caps.requires_template_outside_window is False
