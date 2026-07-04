import pytest
from core.surfaces.envelopes import OutboundMessage
from surfaces.whatsapp.surface import WhatsAppSurface


class _FakeClient:
    def __init__(self): self.sent = []
    async def send_text(self, to, text): self.sent.append((to, text)); return {"messages": [{"id": "x"}]}


@pytest.mark.asyncio
async def test_send_splits_and_sends_each_chunk():
    surf = WhatsAppSurface(_FakeClient())
    surf._client.send_text  # ensure attr
    # 5000 chars -> 2 chunks at 4096
    res = await surf.send(OutboundMessage(session_key="agent:main:whatsapp:dm:15550001111",
                                          text="x" * 5000))
    assert res.success is True
    assert len(surf._client.sent) == 2
    assert surf._client.sent[0][0] == "15550001111"


def test_capabilities_declare_24h_window():
    surf = WhatsAppSurface(_FakeClient())
    caps = surf.capabilities
    assert caps.service_window_secs == 86400
    assert caps.requires_template_outside_window is True
    assert caps.supports_edit is False           # WhatsApp can't edit -> no live streaming
