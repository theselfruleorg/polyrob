"""064 order 0004 — Feishu images and files in and out, offline.

In: an image / file / audio message parses to a Media with a ref; the harness
fetches the bytes from the Open-API host ONLY, with the tenant token (the
executor then absorbs the owner's file and names a correspondent's).
Out: OutboundMessage.media is uploaded and sent after the text; a failed upload
is named and the text has still landed.
"""
import asyncio
import json

import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.feishu.events import parse_event

BOT = "ou_bot0000000000000000000000000000"
USER = "ou_user000000000000000000000000"


def _msg(message_type, content, *, chat_type="p2p", message_id="om_m1", mentions=None):
    return {"schema": "2.0",
            "header": {"event_id": "ev_m", "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": USER}, "sender_type": "user"},
                      "message": {"message_id": message_id, "chat_id": "oc_dm1",
                                  "chat_type": chat_type, "message_type": message_type,
                                  "content": json.dumps(content),
                                  "mentions": mentions or []}}}


@pytest.mark.parametrize("mtype,content,kind,name", [
    ("image", {"image_key": "img_v2_abc"}, "image", None),
    ("file", {"file_key": "file_v2_def", "file_name": "report.pdf"}, "document", "report.pdf"),
    ("audio", {"file_key": "file_v2_aud", "duration": 3000}, "audio", None),
    ("media", {"file_key": "file_v2_vid", "image_key": "img_x", "file_name": "clip.mp4"},
     "video", "clip.mp4"),
])
def test_a_media_message_parses_to_media_with_a_ref(mtype, content, kind, name):
    inbound = parse_event(_msg(mtype, content), BOT)
    assert inbound is not None and inbound.text == ""
    [m] = inbound.media
    key = content.get("file_key") or content.get("image_key")
    assert (m.kind, m.ref, m.filename) == (kind, f"om_m1:{key}", name)
    assert m.data is None                     # bytes are fetched only for a routed turn


def test_a_group_image_without_a_mention_is_dropped():
    assert parse_event(_msg("image", {"image_key": "img_1"}, chat_type="group"), BOT) is None


def test_an_unknown_or_malformed_media_message_is_dropped():
    assert parse_event(_msg("sticker", {"file_key": "x"}), BOT) is None
    assert parse_event(_msg("image", {}), BOT) is None
    assert parse_event(_msg("image", {"image_key": "a:b"}), BOT) is None


# --- fetch ---------------------------------------------------------------------

class _Client:
    base = "https://open.larksuite.com"
    host = "open.larksuite.com"

    def __init__(self):
        self.sent, self.media, self.fail_upload = [], [], None

    def resource_url(self, message_id, file_key, kind):
        from surfaces.feishu.client import FeishuClient
        return FeishuClient.resource_url(self, message_id, file_key, kind)

    async def auth_header(self):
        return {"Authorization": "Bearer t-1"}

    async def send_message(self, target, text):
        self.sent.append((target, text))
        return {"message_id": "om_out"}

    async def send_media(self, target, path, *, image):
        if self.fail_upload:
            raise RuntimeError(self.fail_upload)
        self.media.append((target, path, image))
        return {}


def test_the_harness_fetches_from_the_open_api_host_with_the_tenant_token(monkeypatch):
    import surfaces._shared as shared
    from surfaces.feishu.harness import FeishuHarness
    seen = {}

    async def fake_fetch(url, *, allowed_hosts, headers=None, max_bytes=None):
        seen.update(url=url, hosts=allowed_hosts, headers=headers)
        return b"PNG"

    monkeypatch.setattr(shared, "fetch_capped", fake_fetch)
    h = FeishuHarness.__new__(FeishuHarness)
    h._client = _Client()
    [m] = parse_event(_msg("image", {"image_key": "img_v2_abc"}), BOT).media
    assert asyncio.run(h._fetch_media(m)) == b"PNG"
    assert seen["url"] == ("https://open.larksuite.com/open-apis/im/v1/messages/om_m1"
                           "/resources/img_v2_abc?type=image")
    assert seen["hosts"] == ("open.larksuite.com",)
    assert seen["headers"] == {"Authorization": "Bearer t-1"}


def test_a_file_resource_uses_type_file_and_a_bad_ref_fetches_nothing():
    from core.surfaces.media import Media
    from surfaces.feishu.harness import FeishuHarness
    c = _Client()
    assert c.resource_url("om_1", "file_k", "document").endswith("?type=file")
    h = FeishuHarness.__new__(FeishuHarness)
    h._client = c
    assert asyncio.run(h._fetch_media(Media(kind="image", ref="no-colon"))) is None


def test_fetch_capped_refuses_any_other_host():
    from surfaces._shared import fetch_capped
    out = asyncio.run(fetch_capped("https://evil.example/x", allowed_hosts=("open.larksuite.com",),
                                   headers={"Authorization": "Bearer t-1"}))
    assert out is None


# --- send ----------------------------------------------------------------------

def _surface():
    from surfaces.feishu.surface import FeishuSurface
    c = _Client()
    return FeishuSurface(c), c


def _out(media):
    return OutboundMessage(session_key="feishu:oc_dm1", text="here it is", media=media)


def test_media_out_uploads_after_the_text(tmp_path, monkeypatch):
    import surfaces.feishu.surface as mod
    monkeypatch.setattr(mod, "chat_id_from_session_key", lambda k: "oc_dm1")
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG")
    doc = tmp_path / "r.pdf"
    doc.write_bytes(b"%PDF")
    s, c = _surface()
    assert s.capabilities.media_out is True
    res = asyncio.run(s.send(_out([{"kind": "image", "path": str(img)},
                                   {"kind": "document", "path": str(doc), "caption": "Q3"},
                                   {"subject": "not renderable"}])))
    assert res.success
    assert c.sent[0] == ("oc_dm1", "here it is")
    assert c.media == [("oc_dm1", str(img), True), ("oc_dm1", str(doc), False)]
    assert ("oc_dm1", "Q3") in c.sent


def test_a_failed_upload_is_named_and_the_text_still_lands(tmp_path, monkeypatch):
    import surfaces.feishu.surface as mod
    monkeypatch.setattr(mod, "chat_id_from_session_key", lambda k: "oc_dm1")
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG")
    s, c = _surface()
    c.fail_upload = "code 234006 file too large"
    res = asyncio.run(s.send(_out([{"kind": "image", "path": str(img)},
                                   {"kind": "document", "path": str(tmp_path / "gone.pdf")}])))
    assert res.success
    assert c.sent[0] == ("oc_dm1", "here it is")
    named = c.sent[-1][1]
    assert "Could not attach chart.png: code 234006" in named
    assert "Could not attach gone.pdf: file not found" in named


def test_upload_refuses_an_oversized_file_before_any_call(tmp_path, monkeypatch):
    import surfaces.feishu.client as cmod
    monkeypatch.setattr(cmod, "IMAGE_MAX_BYTES", 3)
    big = tmp_path / "big.png"
    big.write_bytes(b"12345")
    client = cmod.FeishuClient("a", "b")

    async def boom(*a, **k):
        raise AssertionError("no call for an oversized file")

    client.call = boom
    with pytest.raises(RuntimeError, match="over the 3-byte limit"):
        asyncio.run(client.upload(str(big), image=True))


def test_upload_sends_a_multipart_form_and_returns_the_key(tmp_path):
    from surfaces.feishu.client import FeishuClient
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-1")
    client = FeishuClient("a", "b")
    calls = []

    async def fake_call(method, path, *, json=None, params=None, form=None):
        calls.append((method, path, form() if form else None))
        return {"code": 0, "data": {"file_key": "file_v2_new"}}

    client.call = fake_call
    assert asyncio.run(client.upload(str(f), image=False)) == "file_v2_new"
    method, path, form = calls[0]
    assert (method, path) == ("POST", "/open-apis/im/v1/files")
    names = [f[0]["name"] for f in form._fields]
    assert names == ["file_type", "file_name", "file"]
