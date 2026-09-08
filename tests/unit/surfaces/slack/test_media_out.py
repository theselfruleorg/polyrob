"""030 WS-B2/D6: Slack media out via the external-upload flow."""
import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.slack.surface import SlackSurface


class _FakeClient:
    def __init__(self):
        self.messages = []
        self.uploads = []

    async def send_message(self, channel, text, thread_ts=None):
        self.messages.append((channel, text))
        return {"ok": True, "ts": "1.2"}

    async def upload_file(self, channel, path, title=None):
        self.uploads.append({"channel": channel, "path": path, "title": title})
        return {"ok": True}


def test_capabilities_declare_media_out():
    assert SlackSurface(_FakeClient()).capabilities.media_out is True


@pytest.mark.asyncio
async def test_media_entries_are_uploaded_after_the_text(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF fake")
    client = _FakeClient()
    s = SlackSurface(client)
    res = await s.send(OutboundMessage(
        session_key="agent:main:slack:dm:C42:u", text="report attached",
        media=[{"kind": "document", "path": str(f), "caption": "Q3 report"}]))
    assert res.success is True
    assert client.messages == [("C42", "report attached")]
    assert client.uploads == [{"channel": "C42", "path": str(f), "title": "Q3 report"}]


@pytest.mark.asyncio
async def test_upload_failure_never_takes_the_text_down(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"fake")

    class _Boom(_FakeClient):
        async def upload_file(self, channel, path, title=None):
            raise RuntimeError("slack files.getUploadURLExternal failed")

    s = SlackSurface(_Boom())
    res = await s.send(OutboundMessage(
        session_key="agent:main:slack:dm:C42:u", text="hello",
        media=[{"kind": "image", "path": str(f)}]))
    assert res.success is True
