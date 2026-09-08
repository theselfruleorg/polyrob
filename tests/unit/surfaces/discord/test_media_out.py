"""030 WS-B2/D6: Discord media out. The transport supports uploads (≤10MB
attachments) but the surface declared media_out=False and dropped every photo,
document and invoice card with a "surface does not support media" note."""
import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.discord.surface import DiscordSurface


class _FakeClient:
    def __init__(self):
        self.messages = []
        self.files = []

    async def send_message(self, channel_id, text, reply_to=None):
        self.messages.append((channel_id, text))
        return {"id": "m1"}

    async def send_file(self, channel_id, path, filename=None, content=None):
        self.files.append({"channel": channel_id, "path": path,
                           "filename": filename, "content": content})
        return {"id": "f1"}


def test_capabilities_declare_media_out():
    assert DiscordSurface(_FakeClient()).capabilities.media_out is True


@pytest.mark.asyncio
async def test_media_entries_are_uploaded_after_the_text(tmp_path):
    f = tmp_path / "card.png"
    f.write_bytes(b"fake")
    client = _FakeClient()
    s = DiscordSurface(client)
    res = await s.send(OutboundMessage(
        session_key="agent:main:discord:dm:42:u", text="your invoice",
        media=[{"kind": "image", "path": str(f), "caption": "invoice #7"}]))
    assert res.success is True
    assert client.messages == [("42", "your invoice")]
    assert len(client.files) == 1
    assert client.files[0]["path"] == str(f)
    assert client.files[0]["content"] == "invoice #7"


@pytest.mark.asyncio
async def test_missing_file_is_skipped_fail_open(tmp_path):
    client = _FakeClient()
    s = DiscordSurface(client)
    res = await s.send(OutboundMessage(
        session_key="agent:main:discord:dm:42:u", text="hello",
        media=[{"kind": "document", "path": str(tmp_path / "gone.pdf")}]))
    assert res.success is True  # text landed; media fail-open
    assert client.files == []
