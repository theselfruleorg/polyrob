"""OS9 / OS12 / OS14 / OS15 — WhatsApp media kinds, the shared sink seam, the
probe's inbound keys and the media download cap."""
import asyncio
import os

import pytest

from core.surfaces.idempotency import IdempotencyStore
from surfaces.whatsapp.inbound import WhatsAppInbound


class _UD:
    def resolve_internal(self, raw, surface):
        return "u_" + raw


def _parse(tmp_path, m):
    wa = WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")),
                         user_directory=_UD())
    m = {"id": "wamid.1", "from": "15550001111", **m}
    return wa.parse({"entry": [{"changes": [{"value": {"messages": [m]}}]}]})


# --- OS9 ---------------------------------------------------------------------

def test_image_caption_becomes_the_text(tmp_path):
    [msg] = _parse(tmp_path, {"type": "image",
                              "image": {"id": "M1", "mime_type": "image/jpeg",
                                        "caption": "what is this?"}})
    assert msg.text == "what is this?"
    assert msg.media[0].kind == "image" and msg.media[0].ref == "M1"


@pytest.mark.parametrize("mtype,kind", [("document", "document"), ("video", "video"),
                                        ("sticker", "image")])
def test_document_video_sticker_carry_media(tmp_path, mtype, kind):
    body = {"id": "M2", "mime_type": {"document": "application/pdf",
                                      "video": "video/mp4",
                                      "sticker": "image/webp"}[mtype]}
    if mtype == "document":
        body["filename"] = "report.pdf"
    [msg] = _parse(tmp_path, {"type": mtype, mtype: body})
    assert msg.media and msg.media[0].kind == kind
    assert msg.media[0].ref == "M2"
    if mtype == "document":
        assert msg.media[0].filename == "report.pdf"


def test_unsupported_type_is_not_an_empty_turn(tmp_path):
    assert _parse(tmp_path, {"type": "reaction",
                             "reaction": {"message_id": "x", "emoji": "👍"}}) == []


# --- OS12 --------------------------------------------------------------------

def test_sink_is_the_shared_text_sink(tmp_path):
    from surfaces._shared import TextSink
    from surfaces.whatsapp.harness import build_whatsapp_harness
    from tests.unit.surfaces.whatsapp.test_harness_wiring import _Container
    c = _Container()
    build_whatsapp_harness(c, task_agent=object(), data_dir=str(tmp_path))
    assert isinstance(c.get_service("whatsapp_sink"), TextSink)


# --- OS14 --------------------------------------------------------------------

@pytest.mark.parametrize("absent", ["WHATSAPP_WEBHOOK_SECRET", "WHATSAPP_VERIFY_TOKEN"])
def test_probe_is_not_ok_without_inbound_keys(monkeypatch, absent):
    import surfaces.whatsapp.probe as probe_mod

    async def fake_http_json(method, url, **kw):
        return 200, {"display_phone_number": "+1 555"}, ""

    monkeypatch.setattr(probe_mod, "http_json", fake_http_json)
    env = {"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "9",
           "WHATSAPP_WEBHOOK_SECRET": "s", "WHATSAPP_VERIFY_TOKEN": "v"}
    env.pop(absent)
    res = asyncio.run(probe_mod.probe(env))
    assert res.state != "ok"
    assert absent in res.detail


def test_probe_ok_with_all_keys(monkeypatch):
    import surfaces.whatsapp.probe as probe_mod

    async def fake_http_json(method, url, **kw):
        return 200, {"display_phone_number": "+1 555"}, ""

    monkeypatch.setattr(probe_mod, "http_json", fake_http_json)
    env = {"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "9",
           "WHATSAPP_WEBHOOK_SECRET": "s", "WHATSAPP_VERIFY_TOKEN": "v"}
    assert asyncio.run(probe_mod.probe(env)).state == "ok"


# --- OS15 --------------------------------------------------------------------

def test_download_media_refuses_a_body_over_the_cap(monkeypatch):
    import httpx
    from surfaces.whatsapp.client import WhatsAppClient

    big = b"x" * 5000

    def handler(request):
        if request.url.path.endswith("/M1"):
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/f",
                                             "file_size": 10})   # lies
        return httpx.Response(200, content=big)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=transport, **kw))
    client = WhatsAppClient(phone_number_id="9", access_token="t")
    assert asyncio.run(client.download_media("M1", max_bytes=1000)) is None
    assert asyncio.run(client.download_media("M1", max_bytes=10_000)) == big


def test_download_media_refuses_a_declared_size_over_the_cap(monkeypatch):
    import httpx
    from surfaces.whatsapp.client import WhatsAppClient
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/f",
                                         "file_size": 10 ** 9})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=transport, **kw))
    client = WhatsAppClient(phone_number_id="9", access_token="t")
    assert asyncio.run(client.download_media("M1", max_bytes=1000)) is None
    assert len(calls) == 1          # never fetched the body


def test_launch_requires_the_webhook_secret():
    """CHAT-22: without the secret every inbound POST is refused — the
    preflight must say so instead of starting a deaf surface."""
    from surfaces.whatsapp.launch import _REQUIRED
    assert "WHATSAPP_WEBHOOK_SECRET" in _REQUIRED


@pytest.mark.parametrize("host,warn", [("127.0.0.1", False), ("::1", False),
                                       ("localhost", False), ("0.0.0.0", True),
                                       ("10.0.0.2", True)])
def test_plain_http_bind_warning(host, warn):
    from surfaces._launch import plain_http_bind_warning
    assert (plain_http_bind_warning(host) is not None) is warn
