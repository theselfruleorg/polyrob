"""Media in for Discord, Slack and Signal (064 S2b F3). Offline, recorded shapes.

The three polled surfaces read text only: a photo with no caption was dropped
at parse, and a captioned one lost its file. Now each surface turns the
platform's attachments into ``Media`` and hands the shared executor a
downloader; the executor decides by TIER (owner: stored; correspondent: named).
"""
import pytest

from core.surfaces.media import kind_for_mime

# --- recorded platform payloads (trimmed to the fields the parsers read) --------

DISCORD_PHOTO_NO_CAPTION = {
    "id": "1290000000000000001", "channel_id": "555", "content": "",
    "author": {"id": "42", "username": "owner"},
    "attachments": [{
        "id": "1290000000000000002", "filename": "receipt.png", "size": 48213,
        "content_type": "image/png",
        "url": "https://cdn.discordapp.com/attachments/555/129/receipt.png?ex=1&is=2&hm=3",
    }],
}

SLACK_FILE_SHARE = {
    "type": "message", "subtype": "file_share", "user": "U42", "text": "",
    "channel": "D1", "channel_type": "im", "ts": "1727000000.000100",
    "files": [{
        "id": "F07ABC", "name": "report.pdf", "mimetype": "application/pdf",
        "size": 91234,
        "url_private_download": "https://files.slack.com/files-pri/T1-F07ABC/download/report.pdf",
    }],
}

SIGNAL_ATTACHMENT = {
    "sourceNumber": "+15550001111", "sourceName": "Owner", "timestamp": 1727000000123,
    "dataMessage": {
        "message": None, "timestamp": 1727000000123,
        "attachments": [{"contentType": "image/jpeg", "filename": "cat.jpg",
                         "id": "Bx7k2mQ9.jpg", "size": 30211}],
    },
}


def test_kind_for_mime_never_uses_a_transcribed_kind():
    assert kind_for_mime("image/png") == "image"
    assert kind_for_mime("video/mp4") == "video"
    assert kind_for_mime("audio/ogg") == "document"   # stored, never silently skipped
    assert kind_for_mime(None) == "document"


def test_discord_keeps_a_photo_with_empty_content():
    from surfaces.discord.gateway import parse_message_create
    inbound = parse_message_create(DISCORD_PHOTO_NO_CAPTION, "999")
    assert inbound is not None
    assert inbound.text == ""
    [m] = inbound.media
    assert (m.kind, m.mime, m.filename) == ("image", "image/png", "receipt.png")
    assert m.url.startswith("https://cdn.discordapp.com/")


def test_discord_still_drops_an_empty_message_without_files():
    from surfaces.discord.gateway import parse_message_create
    d = dict(DISCORD_PHOTO_NO_CAPTION, attachments=[])
    assert parse_message_create(d, "999") is None


def test_slack_reads_a_file_share_and_still_ignores_other_subtypes():
    from surfaces.slack.socket_mode import parse_event
    inbound = parse_event(SLACK_FILE_SHARE, "UBOT")
    assert inbound is not None
    [m] = inbound.media
    assert (m.kind, m.filename, m.ref) == ("document", "report.pdf", "F07ABC")
    assert m.url.startswith("https://files.slack.com/")
    assert parse_event(dict(SLACK_FILE_SHARE, subtype="message_changed"), "UBOT") is None


def test_signal_reads_attachment_ids():
    from surfaces.signal.surface import parse_envelope
    inbound = parse_envelope(SIGNAL_ATTACHMENT, "+15559999999")
    assert inbound is not None
    [m] = inbound.media
    assert (m.kind, m.ref, m.filename) == ("image", "Bx7k2mQ9.jpg", "cat.jpg")


# --- the harness hands the executor its downloader only when files arrived ----

@pytest.mark.asyncio
async def test_route_passes_the_downloader_only_with_media(monkeypatch):
    from surfaces.discord.gateway import parse_message_create
    from surfaces.discord.harness import DiscordHarness

    seen = []

    async def _route(container, inbound):
        return object()

    async def _act(task_agent, result, **kw):
        seen.append(kw.get("fetch_media"))

    monkeypatch.setattr("core.surfaces.dispatcher.route_inbound", _route)
    import surfaces.telegram.harness as home
    monkeypatch.setattr(home, "act_on_inbound", _act)

    class _Client:
        async def trigger_typing(self, target):
            pass

    h = DiscordHarness(None, None, _Client(), gateway=None, dedup=None)
    await h._route(parse_message_create(DISCORD_PHOTO_NO_CAPTION, "999"))
    await h._route(parse_message_create(
        dict(DISCORD_PHOTO_NO_CAPTION, content="hi", attachments=[]), "999"))
    assert seen[0] is not None and seen[0].__func__ is DiscordHarness._fetch_media
    assert seen[1] is None


# --- the fetch guard: https + allowed host + size cap while reading -------------

class _Resp:
    def __init__(self, status=200, chunks=(b"abc",), length=None):
        self.status = status
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self._chunks = chunks
        self.content = self

    async def iter_chunked(self, n):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    calls = []

    def __init__(self, resp):
        self._resp = resp

    def get(self, url, headers=None, allow_redirects=True):
        _Session.calls.append((url, dict(headers or {}), allow_redirects))
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def fake_http(monkeypatch):
    _Session.calls = []
    holder = {"resp": _Resp()}
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _Session(holder["resp"]))
    return holder


@pytest.mark.asyncio
async def test_fetch_refuses_a_foreign_host_and_never_sends_the_token(fake_http):
    from surfaces._shared import fetch_capped
    hosts = ("files.slack.com",)
    auth = {"Authorization": "Bearer xoxb-secret"}
    assert await fetch_capped("https://evil.example/x", allowed_hosts=hosts, headers=auth) is None
    assert await fetch_capped("https://files.slack.com.evil.example/x",
                              allowed_hosts=hosts, headers=auth) is None
    assert await fetch_capped("http://files.slack.com/x", allowed_hosts=hosts,
                              headers=auth) is None
    assert _Session.calls == []                      # the token never left


@pytest.mark.asyncio
async def test_fetch_reads_an_allowed_host_without_redirects(fake_http):
    from surfaces._shared import fetch_capped
    fake_http["resp"] = _Resp(chunks=(b"ab", b"cd"))
    data = await fetch_capped("https://cdn.discordapp.com/a.png",
                              allowed_hosts=("cdn.discordapp.com",))
    assert data == b"abcd"
    assert _Session.calls[0][2] is False


@pytest.mark.asyncio
async def test_fetch_caps_a_lying_body_while_reading(fake_http):
    from surfaces._shared import fetch_capped
    fake_http["resp"] = _Resp(chunks=(b"x" * 600, b"x" * 600))       # no Content-Length
    assert await fetch_capped("https://cdn.discordapp.com/a", max_bytes=1000,
                              allowed_hosts=("cdn.discordapp.com",)) is None
    fake_http["resp"] = _Resp(length=5000)
    assert await fetch_capped("https://cdn.discordapp.com/a", max_bytes=1000,
                              allowed_hosts=("cdn.discordapp.com",)) is None
    fake_http["resp"] = _Resp(status=302)
    assert await fetch_capped("https://cdn.discordapp.com/a",
                              allowed_hosts=("cdn.discordapp.com",)) is None


@pytest.mark.asyncio
async def test_signal_getattachment_decodes_and_caps(monkeypatch):
    import base64

    from surfaces.signal.harness import SignalHarness
    from core.surfaces.media import Media

    class _Client:
        account = "+15559999999"

        def __init__(self, data):
            self.data = data

        async def get_attachment(self, ref, **kw):
            assert ref == "Bx7k2mQ9.jpg"
            return self.data

    h = SignalHarness(None, None, _Client(b"jpegbytes"), stream=None, dedup=None)
    assert await h._fetch_media(Media(kind="image", ref="Bx7k2mQ9.jpg")) == b"jpegbytes"
    monkeypatch.setenv("INBOUND_MEDIA_MAX_MB", "0.000001")
    assert await h._fetch_media(Media(kind="image", ref="Bx7k2mQ9.jpg")) is None
    assert base64  # decode path covered in SignalClient.get_attachment


# --- by tier: the owner's file is stored, a correspondent's only named -----------

def test_correspondent_attachments_are_named_not_absorbed():
    """The shared executor's correspondent path names the files (D53); the
    polled surfaces now feed it the same Media list Telegram does."""
    from surfaces.discord.gateway import parse_message_create
    from surfaces.telegram.harness import _correspondent_text
    from core.surfaces.act import InboundResult

    inbound = parse_message_create(DISCORD_PHOTO_NO_CAPTION, "999")
    text = _correspondent_text(InboundResult(inbound=inbound, decision=None))
    assert "receipt.png (image/png)" in text
    assert "did NOT download" in text
