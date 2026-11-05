"""A raw payload is checked before it reaches the heap (MCP body, IMAP message, file copy)."""
import json
import tracemalloc
import types

import pytest

from tools.mcp import http_reply
from tools.mcp.http_reply import read_rpc_reply


class _Content:
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    def __aiter__(self):
        async def _lines():
            for chunk in self.chunks:
                self.read += 1
                yield chunk
        return _lines()


def _response(ctype, chunks):
    return types.SimpleNamespace(headers={"Content-Type": ctype}, content=_Content(chunks),
                                 charset=None)


@pytest.mark.asyncio
async def test_a_plain_body_within_the_cap_parses():
    body = json.dumps({"jsonrpc": "2.0", "id": "7", "result": {"ok": 1}}).encode()
    reply = await read_rpc_reply(_response("application/json", [body[:5], body[5:]]), "7")
    assert reply["result"] == {"ok": 1}


@pytest.mark.asyncio
async def test_a_plain_body_over_the_cap_stops_reading_and_names_the_cap(monkeypatch):
    monkeypatch.setattr(http_reply, "MAX_REPLY_BYTES", 10)
    response = _response("application/json", [b"x" * 8] * 100)
    with pytest.raises(ValueError, match="over the 10 byte limit"):
        await read_rpc_reply(response, "7")
    assert response.content.read == 2, "the rest of the body never reached memory"


@pytest.mark.asyncio
async def test_an_sse_event_over_the_cap_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(http_reply, "MAX_REPLY_BYTES", 10)
    response = _response("text/event-stream", [b"data: 12345678\n"] * 100)
    with pytest.raises(ValueError, match="over the 10 byte limit"):
        await read_rpc_reply(response, "7")
    assert response.content.read == 1


@pytest.mark.asyncio
async def test_many_small_sse_events_do_not_add_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(http_reply, "MAX_REPLY_BYTES", 40)
    done = json.dumps({"id": "7", "result": 1}).encode()
    chunks = [b'data: {"method":"n"}\n', b"\n"] * 20 + [b"data: " + done + b"\n", b"\n"]
    assert (await read_rpc_reply(_response("text/event-stream", chunks), "7"))["result"] == 1


def _mail_tool(conn):
    from tools.email_tool import EmailTool
    cfg = types.SimpleNamespace(
        gmail_email="bot@example.com", gmail_app_password="app-pw", gmail_smtp_server="smtp.test",
        gmail_smtp_port=587, gmail_imap_server="imap.test")
    tool = EmailTool(name="email", config=cfg, container=None)
    tool._initialized = True
    tool._enabled = True
    tool._mbox_conn = conn
    return tool


class _Conn:
    def __init__(self, size):
        self.size = size
        self.fetched = []

    def select(self, *_a, **_k):
        return "OK", [b"1"]

    def uid(self, command, uid, what):
        self.fetched.append(what)
        if what == "(RFC822.SIZE)":
            return "OK", [b"1 (UID 42 RFC822.SIZE %d)" % self.size]
        return "OK", [(b"1 (UID 42 BODY[] {30}", b"From: a@x\r\nSubject: s\r\n\r\nhello\r\n"), b")"]


@pytest.mark.asyncio
async def test_a_message_over_the_cap_is_refused_before_the_body_fetch():
    from tools.email_mailbox import MAX_MESSAGE_BYTES
    from tools.email_mailbox import EmailReadAction
    conn = _Conn(MAX_MESSAGE_BYTES + 1)
    result = await _mail_tool(conn).email_read(EmailReadAction(uid="42"))
    assert conn.fetched == ["(RFC822.SIZE)"]
    assert "over the" in result.error and "stays in the mailbox" in result.error


@pytest.mark.asyncio
async def test_a_message_within_the_cap_reads():
    from tools.email_mailbox import EmailReadAction
    conn = _Conn(100)
    result = await _mail_tool(conn).email_read(EmailReadAction(uid="42"))
    assert conn.fetched == ["(RFC822.SIZE)", "(BODY.PEEK[])"]
    assert "hello" in result.extracted_content


class _NoSizeConn(_Conn):
    def uid(self, command, uid, what):
        if what == "(RFC822.SIZE)":
            self.fetched.append(what)
            return "OK", [None]
        return super().uid(command, uid, what)


@pytest.mark.asyncio
async def test_a_server_with_no_size_answer_still_reads():
    from tools.email_mailbox import EmailReadAction
    result = await _mail_tool(_NoSizeConn(0)).email_read(EmailReadAction(uid="42"))
    assert "hello" in result.extracted_content


def _peak_during(fn):
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_copy_file_streams_and_is_exact(tmp_path):
    from core.security.workspace_io import copy_file
    src, dst = tmp_path / "a.bin", tmp_path / "b.bin"
    payload = b"0123456789abcdef" * (512 * 1024)  # 8 MiB
    src.write_bytes(payload)
    written = []
    peak = _peak_during(lambda: written.append(copy_file(str(src), str(dst), str(tmp_path))))
    assert dst.read_bytes() == payload
    assert written == [len(payload)]
    assert peak < 3 * 1024 * 1024


def test_copy_file_refuses_a_source_outside_the_root(tmp_path):
    from core.security.workspace_io import copy_file
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret").write_bytes(b"x")
    (root / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(OSError):
        copy_file(str(root / "link"), str(root / "out"), str(root))
    assert not (root / "out").exists()


def test_copy_file_failure_keeps_the_old_destination(tmp_path, monkeypatch):
    from core.security import workspace_io
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")

    def _boom(_in, _out, _size):
        raise OSError("disk full")

    monkeypatch.setattr(workspace_io.shutil, "copyfileobj", _boom)
    with pytest.raises(OSError):
        workspace_io.copy_file(str(src), str(dst), str(tmp_path))
    assert dst.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]


def test_write_bytes_still_keeps_an_existing_mode(tmp_path):
    from core.security.workspace_io import write_bytes
    target = tmp_path / "a"
    target.write_bytes(b"x")
    target.chmod(0o640)
    write_bytes(str(target), str(tmp_path), b"y")
    assert target.read_bytes() == b"y"
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_a_long_sse_line_is_read_on_a_real_session():
    """A large tool result is one data: line, and aiohttp's default refuses it."""
    import json

    import aiohttp
    from aiohttp import web

    async def _stream(request):
        reply = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await reply.prepare(request)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"t": "x" * 5_000_000}})
        await reply.write(b"data: " + body.encode() + b"\n\n")
        return reply
    app = web.Application()
    app.router.add_get("/", _stream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(read_bufsize=http_reply.READ_BUFSIZE) as session:
            async with session.get(f"http://127.0.0.1:{port}/") as response:
                found = await read_rpc_reply(response, 1)
    finally:
        await runner.cleanup()
    assert len(found["result"]["t"]) == 5_000_000


@pytest.mark.asyncio
async def test_a_refused_reply_reads_only_its_start(monkeypatch):
    monkeypatch.setattr(http_reply, "ERROR_BODY_MAX_BYTES", 8)
    response = _response("text/plain", [b"x" * 8] * 100)
    assert await http_reply.read_error_text(response) == "x" * 8
    assert response.content.read == 1


@pytest.mark.asyncio
async def test_an_unknown_charset_does_not_refuse_a_good_reply():
    response = _response("application/json", [b'{"id": "7", "result": 1}'])
    response.charset = "no-such-charset"
    assert (await read_rpc_reply(response, "7"))["result"] == 1
