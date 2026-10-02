"""An EXECUTING console money verb (`/send … go`, `/bridge … go`) never holds
the HTTP request.

The console passed no ``deliver``, so the harness's ``_runs_in_background``
rule never fired and the request waited up to ~120 s for a receipt. It now
answers "started" at once under the SAME rule Telegram uses, and the result
arrives later on the owner-notice rail and on the chat's live socket.
"""
import asyncio

import pytest


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Container:
    def __init__(self, d):
        self.config = _Cfg(d)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, d):
        self.container = _Container(d)


class _Sio:
    def __init__(self):
        self.emitted = []

    async def emit(self, name, event, room=None):
        self.emitted.append((name, event, room))


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    import surfaces.telegram.harness as harness
    import webview.server as server
    import core.surfaces.user_delivery as ud

    release = asyncio.Event()
    admin_calls = []

    async def _slow_admin(task_agent, result, cmd):
        admin_calls.append(cmd)
        await release.wait()           # a receipt that takes "minutes"
        return "Sent 0.1 ETH — tx 0xabc confirmed."
    monkeypatch.setattr(harness, "_handle_owner_admin", _slow_admin)

    notices = []

    async def _deliver_user_message(container, user_id, text, **kw):
        notices.append((user_id, text, kw))
        return "no_sink"
    monkeypatch.setattr(ud, "deliver_user_message", _deliver_user_message)

    sio = _Sio()
    monkeypatch.setattr(server, "_sio", sio)
    return {"release": release, "admin": admin_calls, "notices": notices,
            "sio": sio, "agent": _Agent(str(tmp_path))}


def test_send_go_answers_started_and_delivers_the_result_later(rig):
    from webview.console_commands import maybe_handle_console_command

    async def _run():
        reply = await asyncio.wait_for(
            maybe_handle_console_command(rig["agent"], "sess1", "rob",
                                         "/send 0.1 eth 0x" + "11" * 20 + " go"),
            timeout=2)
        # The request returned while the verb is still waiting on its receipt.
        assert "started" in reply.lower()
        assert not rig["notices"]
        rig["release"].set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if rig["notices"] and rig["sio"].emitted:
                break
        return reply
    asyncio.run(_run())
    assert rig["admin"] == ["/send"]
    user_id, text, kw = rig["notices"][0]
    assert user_id == "rob" and "0xabc" in text
    assert kw.get("session_id") == "sess1"
    name, event, room = rig["sio"].emitted[0]
    assert name == "feed_update" and room == "sess1"
    assert event["type"] == "command_reply" and "0xabc" in event["data"]["text"]


def test_a_quote_without_go_still_answers_inline(rig):
    """The bare form is a quote: no background, the answer is the reply."""
    from webview.console_commands import maybe_handle_console_command
    rig["release"].set()
    reply = asyncio.run(maybe_handle_console_command(
        rig["agent"], "sess1", "rob", "/send 0.1 eth 0x" + "11" * 20))
    assert "0xabc" in (reply or "")
    assert not rig["notices"] and not rig["sio"].emitted


def test_cold_open_delivers_only_the_owner_notice(rig):
    """No session (the cold-open path): the durable owner notice still lands."""
    from webview.console_commands import maybe_handle_console_command

    async def _run():
        reply = await maybe_handle_console_command(
            rig["agent"], "", "rob", "/bridge 0.1 eth base robinhood go")
        rig["release"].set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if rig["notices"]:
                break
        return reply
    reply = asyncio.run(_run())
    assert "started" in reply.lower()
    assert rig["notices"] and not rig["sio"].emitted
