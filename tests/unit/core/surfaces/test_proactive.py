# tests/unit/core/surfaces/test_proactive.py
import pytest
from core.surfaces.send_policy import SendDecision
from core.surfaces.proactive import resolve_proactive_send


class _Surf:
    def __init__(self, d): self._d = d
    def can_send_now(self, sk, *, now=None): return self._d


class _Container:
    def __init__(self, surfaces): self._s = surfaces
    def get_service(self, k):
        return {"webhook_surfaces": self._s}.get(k)


@pytest.mark.asyncio
async def test_allow_maps_to_send():
    c = _Container({"whatsapp": _Surf(SendDecision.ALLOW)})
    assert (await resolve_proactive_send(c, "whatsapp", "sk", "hi"))[0] == "send"


@pytest.mark.asyncio
async def test_template_only_maps_to_template():
    c = _Container({"whatsapp": _Surf(SendDecision.TEMPLATE_ONLY)})
    assert (await resolve_proactive_send(c, "whatsapp", "sk", "hi"))[0] == "template"


@pytest.mark.asyncio
async def test_unknown_surface_defaults_to_send():
    c = _Container({})
    assert (await resolve_proactive_send(c, "telegram", "sk", "hi"))[0] == "send"
