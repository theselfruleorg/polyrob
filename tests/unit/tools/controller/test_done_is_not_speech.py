"""`done` is not speech — only `send_message` reaches the user (contract C1).

Until 2026-09-17 `done` had a router mirror that published its text whenever
`send_message` had not claimed the turn. The `message` tool (a file with a
caption) never claimed it, so on prod the owner received the answer AND a
2,400-char third-person session recap after every such turn. Before that the
same net leaked a session recap into a public group. There is no flag: the
prompt promises `send_message` is the only verb the user reads, and the code
now has no path that could break that promise.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.surfaces import turn_reply

ROOT = Path(__file__).resolve().parents[4]


def _done_body() -> str:
    src = (ROOT / "tools" / "controller" / "action_registration.py").read_text()
    start = src.index("async def done(params: DoneAction")
    end = src.index("elif self.output_model is not None", start)
    return src[start:end]


def test_done_has_no_delivery_path():
    """Ratchet. `done` may write history and the session feed (the run log);
    it may not reach a router, a surface or the discrete mirror."""
    body = _done_body()
    for forbidden in ("build_discrete_publish", "build_completion_publish",
                      "_message_router", "router.publish", "user_delivery",
                      "maybe_deliver_autonomous_send"):
        assert forbidden not in body, f"done() reaches a delivery seam: {forbidden}"


def test_the_completion_mirror_is_gone():
    from core.surfaces import outbound_mirror
    assert not hasattr(outbound_mirror, "build_completion_publish")


def test_no_flag_governs_done_delivery():
    """A flag here would be a second knob on a rule the prompt states without
    one. `CHAT_SINGLE_FINAL` (the latch gate) and the short-lived
    `DONE_REPLY_FALLBACK` are both retired."""
    from core.flags_catalog import CATALOG
    names = {row[0] for row in CATALOG}
    assert "CHAT_SINGLE_FINAL" not in names
    assert "DONE_REPLY_FALLBACK" not in names


class _Router:
    def __init__(self):
        self.published = []

    async def publish(self, msg):
        self.published.append(msg)
        return True


@pytest.mark.asyncio
async def test_send_message_is_the_voice_and_records_the_reply(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    orch = SimpleNamespace(_message_router=router, _chat_session_key="telegram:1")
    await build_discrete_publish(router, "telegram:1")("Posted. Link: …")
    turn_reply.mark_reply_published(orch, "Posted. Link: …")
    assert [m.text for m in router.published] == ["Posted. Link: …"]
    assert turn_reply.last_reply_text(orch) == "Posted. Link: …"
