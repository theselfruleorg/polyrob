"""AgentMailClient — HTTP managed-inbox provider (Task 3, 2026-08-18 plan).

All tests run against httpx.MockTransport — no network.
"""
import json
import re

import httpx
import pytest

from core.exceptions import APIError
from tools.email_providers.agentmail import AgentMailClient


def _client(tmp_path, handler):
    return AgentMailClient(
        "am_test_key", data_home=tmp_path,
        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_provision_posts_and_persists(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/v0/inboxes"
        assert request.headers["Authorization"] == "Bearer am_test_key"
        body = json.loads(request.content)
        assert body["client_id"] == "polyrob-rob"
        return httpx.Response(200, json={
            "pod_id": "pod_1", "inbox_id": "rob@agentmail.to",
            "email": "rob@agentmail.to",
            "created_at": "2026-08-18T00:00:00Z", "updated_at": "2026-08-18T00:00:00Z",
        })

    c = _client(tmp_path, handler)
    state = await c.provision("rob")
    assert state["address"] == "rob@agentmail.to"
    on_disk = json.loads((tmp_path / "agent_mail.json").read_text())
    assert on_disk["inbox_id"] == "rob@agentmail.to"
    assert on_disk["address"] == "rob@agentmail.to"

    # Second call short-circuits on the persisted state — no second POST.
    await c.provision("rob")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_mints_message_id_and_records_thread(tmp_path):
    seen = {}

    def handler(request):
        if request.url.path == "/v0/inboxes":
            return httpx.Response(200, json={
                "pod_id": "p", "inbox_id": "rob@agentmail.to", "email": "rob@agentmail.to",
                "created_at": "x", "updated_at": "x"})
        assert request.url.path == "/v0/inboxes/rob@agentmail.to/messages/send"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "am_msg_1", "thread_id": "th_1"})

    c = _client(tmp_path, handler)
    await c.provision("rob")
    mid = await c.send("to@example.com", "Hi", "Body text")

    assert re.match(r"^<.+@agentmail\.to>$", mid)
    assert seen["body"]["to"] == "to@example.com"
    assert seen["body"]["subject"] == "Hi"
    assert seen["body"]["text"] == "Body text"
    assert seen["body"]["headers"]["Message-ID"] == mid
    # thread map records provider thread -> our minted RFC mid
    assert c.minted_mid_for_thread("th_1") == mid


@pytest.mark.asyncio
async def test_send_reply_sets_threading_headers(tmp_path):
    seen = {}

    def handler(request):
        if request.url.path == "/v0/inboxes":
            return httpx.Response(200, json={
                "pod_id": "p", "inbox_id": "in_1", "email": "rob@agentmail.to",
                "created_at": "x", "updated_at": "x"})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "m", "thread_id": "t"})

    c = _client(tmp_path, handler)
    await c.provision("rob")
    await c.send("to@example.com", "Re: Hi", "Body",
                 in_reply_to="<abc@example.com>")
    headers = seen["body"]["headers"]
    assert headers["In-Reply-To"] == "<abc@example.com>"
    assert headers["References"] == "<abc@example.com>"


@pytest.mark.asyncio
async def test_list_and_get(tmp_path):
    def handler(request):
        if request.url.path == "/v0/inboxes":
            return httpx.Response(200, json={
                "pod_id": "p", "inbox_id": "in_1", "email": "rob@agentmail.to",
                "created_at": "x", "updated_at": "x"})
        if request.url.path == "/v0/inboxes/in_1/messages":
            return httpx.Response(200, json={"messages": [
                {"message_id": "m1", "thread_id": "t1", "from": "a@b.c",
                 "subject": "s", "timestamp": "2026-08-18T00:00:00Z"}]})
        if request.url.path == "/v0/inboxes/in_1/messages/m1":
            return httpx.Response(200, json={
                "message_id": "m1", "thread_id": "t1", "from": "a@b.c",
                "subject": "s", "text": "full text", "extracted_text": "reply only",
                "in_reply_to": "<x@y.z>", "timestamp": "2026-08-18T00:00:00Z"})
        raise AssertionError(f"unexpected path {request.url.path}")

    c = _client(tmp_path, handler)
    await c.provision("rob")
    msgs = await c.list_messages(limit=5)
    assert msgs[0]["message_id"] == "m1"
    full = await c.get_message("m1")
    assert full["extracted_text"] == "reply only"


@pytest.mark.asyncio
async def test_http_error_raises_apierror(tmp_path):
    def handler(request):
        if request.url.path == "/v0/inboxes":
            return httpx.Response(429, json={"error": "rate limited"})
        raise AssertionError

    c = _client(tmp_path, handler)
    with pytest.raises(APIError):
        await c.provision("rob")


def test_repr_hides_api_key(tmp_path):
    c = AgentMailClient("am_secret_key", data_home=tmp_path)
    assert "am_secret_key" not in repr(c)
    assert "am_secret_key" not in str(c)
