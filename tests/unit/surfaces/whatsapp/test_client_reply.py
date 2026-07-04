import pytest

from surfaces.whatsapp.client import WhatsAppClient


def _client():
    c = WhatsAppClient(phone_number_id="pnid", access_token="tok")
    captured = {}

    async def fake_post(payload):
        captured.clear()
        captured.update(payload)
        return {}

    c._post = fake_post
    return c, captured


@pytest.mark.asyncio
async def test_send_text_no_reply_has_no_context():
    c, captured = _client()
    await c.send_text("555", "hi")
    assert "context" not in captured
    assert captured["text"] == {"body": "hi"}


@pytest.mark.asyncio
async def test_send_text_with_reply_adds_context():
    c, captured = _client()
    await c.send_text("555", "hi", reply_to="wamid.7")
    assert captured["context"] == {"message_id": "wamid.7"}


@pytest.mark.asyncio
async def test_mark_read_payload():
    c, captured = _client()
    await c.mark_read("wamid.7")
    assert captured == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.7"}
