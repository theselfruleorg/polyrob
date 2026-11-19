"""Surface push for the avatar (modules/avatar/push.py).

X, Discord and Telegram (bot + group photo) are live pushes. Live paths are
mocked — no network.
"""
import pytest

from modules.avatar import push


def test_sha256_file_is_stable(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nhello")
    h1 = push.sha256_file(p)
    assert h1 == push.sha256_file(p)
    assert len(h1) == 64


def test_telegram_instructions_mention_botfather_and_path(tmp_path):
    p = tmp_path / "pfp.png"
    p.write_bytes(b"x")
    text = push.telegram_instructions(p)
    assert "BotFather" in text
    assert "/setuserpic" in text
    assert str(p) in text


def test_build_twitter_api_raises_without_creds():
    with pytest.raises(push.TwitterCredsMissing):
        push.build_twitter_api(env={})  # no OAuth1 creds -> clear error, no tweepy needed


def test_push_twitter_calls_update_profile_image(tmp_path):
    p = tmp_path / "pfp.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    class _Api:
        def __init__(self):
            self.called_with = None

        def update_profile_image(self, filename=None):
            self.called_with = filename

    api = _Api()
    push.push_twitter(p, api=api)
    assert api.called_with == str(p)


def test_build_twitter_api_signs_a_multipart_v11_post(tmp_path, monkeypatch):
    pytest.importorskip("requests_oauthlib")
    from requests_oauthlib import OAuth1Session
    p = tmp_path / "pfp.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

    def fake_send(self, request, **kw):
        seen["url"], seen["auth"], seen["body"] = request.url, request.headers["Authorization"], request.body
        return _Resp()

    monkeypatch.setattr(OAuth1Session, "send", fake_send)
    env = {"TWITTER_API_KEY": "ck", "TWITTER_API_SECRET_KEY": "cs",
           "TWITTER_ACCESS_TOKEN": "at", "TWITTER_ACCESS_TOKEN_SECRET": "as"}
    push.push_twitter(p, env=env)
    assert seen["url"] == "https://api.twitter.com/1.1/account/update_profile_image.json"
    auth = seen["auth"].decode() if isinstance(seen["auth"], bytes) else seen["auth"]
    assert auth.startswith("OAuth ") and 'oauth_consumer_key="ck"' in auth and 'oauth_token="at"' in auth
    assert b'name="image"; filename="pfp.png"' in seen["body"]


def test_push_discord_raises_without_token():
    with pytest.raises(push.DiscordCredsMissing):
        push.push_discord("/nonexistent.png", env={})


def test_push_discord_patches_users_me_with_data_uri(tmp_path):
    p = tmp_path / "pfp.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    seen = {}

    def opener(req):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = req.data
        import contextlib
        return contextlib.nullcontext()

    push.push_discord(p, env={"DISCORD_BOT_TOKEN": "tok123"}, opener=opener)
    assert seen["url"] == "https://discord.com/api/v10/users/@me"
    assert seen["method"] == "PATCH"
    assert seen["auth"] == "Bot tok123"
    import json as _json
    body = _json.loads(seen["body"].decode("utf-8"))
    assert body["avatar"].startswith("data:image/png;base64,")


def test_an_svg_avatar_is_refused_before_any_push(tmp_path):
    p = tmp_path / "avatar.svg"
    p.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    class _Api:
        def update_profile_image(self, filename=None):
            raise AssertionError("must not be called for an SVG")

    with pytest.raises(push.NotRaster):
        push.push_twitter(p, api=_Api())
    with pytest.raises(push.NotRaster):
        push.push_discord(p, env={"DISCORD_BOT_TOKEN": "t"},
                          opener=lambda r: (_ for _ in ()).throw(AssertionError()))


def _real_png(tmp_path, mode="RGBA"):
    from PIL import Image
    p = tmp_path / "pfp.png"
    Image.new(mode, (8, 8), (10, 20, 30, 0) if mode == "RGBA" else (10, 20, 30)).save(p)
    return p


def test_to_jpeg_flattens_transparency(tmp_path):
    data = push.to_jpeg(_real_png(tmp_path))
    assert data[:3] == b"\xff\xd8\xff"


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_push_telegram_raises_without_token(tmp_path):
    with pytest.raises(push.TelegramCredsMissing):
        push.push_telegram_bot(_real_png(tmp_path), env={})


def test_push_telegram_bot_posts_a_static_jpeg(tmp_path):
    seen = {}

    def opener(req):
        seen["url"], seen["body"] = req.full_url, req.data
        return _Resp(b'{"ok": true, "result": true}')

    push.push_telegram_bot(_real_png(tmp_path), env={"TELEGRAM_BOT_TOKEN": "T"}, opener=opener)
    assert seen["url"] == "https://api.telegram.org/botT/setMyProfilePhoto"
    assert b'"type": "static"' in seen["body"] and b"attach://avatar" in seen["body"]
    assert b"image/jpeg" in seen["body"] and b"\xff\xd8\xff" in seen["body"]


def test_push_telegram_chat_names_the_chat_and_surfaces_the_error(tmp_path):
    seen = {}

    def opener(req):
        seen["url"], seen["body"] = req.full_url, req.data
        return _Resp(b'{"ok": false, "description": "Bad Request: not enough rights"}')

    with pytest.raises(RuntimeError, match="not enough rights"):
        push.push_telegram_chat(_real_png(tmp_path, "RGB"), "-1001",
                                env={"TELEGRAM_BOT_TOKEN": "T"}, opener=opener)
    assert seen["url"].endswith("/setChatPhoto")
    assert b'name="chat_id"\r\n\r\n-1001' in seen["body"]


# --- 120 W1: the ONE push procedure ------------------------------------------

PNG_IMG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


@pytest.fixture
def slot(tmp_path, monkeypatch):
    from core.avatar import set_avatar
    set_avatar(tmp_path, "alice", PNG_IMG, source="file:f.png")
    return tmp_path


def test_a_room_photo_alone_is_the_room_photo_only(slot, monkeypatch):
    """A room named alone changes that room: the bot's own photo is a public
    face the caller did not name."""
    from modules.avatar import push
    calls = []
    monkeypatch.setattr(push, "push_telegram_bot", lambda p: calls.append("bot"))
    monkeypatch.setattr(push, "push_telegram_chat", lambda p, c: calls.append(c))
    monkeypatch.setattr(push, "push_twitter", lambda p: calls.append("x"))
    monkeypatch.setattr(push, "push_discord", lambda p: calls.append("discord"))
    res = push.push_avatar(slot, "alice", telegram_chats=["-1001", "-1001"])
    assert [r.surface for r in res] == ["telegram:-1001"] and calls == ["-1001"]


def test_telegram_named_with_a_room_sets_the_bot_and_the_room(slot, monkeypatch):
    from modules.avatar import push
    calls = []
    monkeypatch.setattr(push, "push_telegram_bot", lambda p: calls.append("bot"))
    monkeypatch.setattr(push, "push_telegram_chat", lambda p, c: calls.append(c))
    res = push.push_avatar(slot, "alice", surfaces=["telegram"], telegram_chats=["-1001"])
    assert [r.surface for r in res] == ["telegram", "telegram:-1001"] and calls == ["bot", "-1001"]


def test_the_record_takes_the_cross_process_lock(slot, monkeypatch):
    from modules.avatar import push
    import core.state.files as files
    seen = []
    real = files.file_lock

    def spy(path):
        seen.append(str(path))
        return real(path)
    monkeypatch.setattr(files, "file_lock", spy)
    push._record_pushed(slot, "alice", "twitter", "abc")
    assert seen and seen[0].endswith("pushed.json")
    assert push.load_pushed(slot, "alice")["twitter"] == {"hash": "abc"}


def test_normalize_surfaces_reads_x_and_names_the_unknown():
    from modules.avatar import push
    assert push.normalize_surfaces(["X", "telegram", "x", ""]) == (("twitter", "telegram"), ())
    assert push.normalize_surfaces(["mastodon"]) == ((), ("mastodon",))


def test_an_unknown_surface_stops_everything(slot):
    from modules.avatar import push
    res = push.push_avatar(slot, "alice", surfaces=["x", "mastodon"])
    assert [r.status for r in res] == ["refused"] and "mastodon" in res[0].text


def test_an_unreadable_slot_is_not_an_empty_one(slot):
    from core.avatar import avatar_dir
    from modules.avatar import push
    (avatar_dir(slot, "alice") / "avatar.json").write_text("{not json")
    res = push.push_avatar(slot, "alice")
    assert res[0].status == "refused" and "unreadable" in res[0].text


def test_each_surface_reports_alone_and_only_a_success_is_recorded(slot, monkeypatch):
    from modules.avatar import push

    def _boom(p):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(push, "push_twitter", _boom)
    monkeypatch.setattr(push, "push_discord", lambda p: None)

    def _no_token(p):
        raise push.TelegramCredsMissing("missing TELEGRAM_BOT_TOKEN")

    monkeypatch.setattr(push, "push_telegram_bot", _no_token)
    res = {r.surface: r for r in push.push_avatar(
        slot, "alice", surfaces=["x", "discord", "telegram"], telegram_chats=["-1001"])}
    assert res["twitter"].status == "failed" and "Set it manually" in res["twitter"].text
    assert res["discord"].status == "updated"
    assert res["telegram"].status == "no_credentials"
    assert "telegram:-1001" not in res                       # one token: not repeated
    assert set(push.load_pushed(slot, "alice")) == {"discord"}


def test_the_record_merges_a_concurrent_writer(slot):
    """Two writers (the CLI and the service): the second merges, never overwrites."""
    from modules.avatar import push
    push._record_pushed(slot, "alice", "twitter", "aa")
    push._record_pushed(slot, "alice", "discord", "bb")
    assert push.load_pushed(slot, "alice") == {"twitter": {"hash": "aa"},
                                               "discord": {"hash": "bb"}}
    assert not list((slot / "identity" / "alice" / "avatar").glob("*.tmp"))


def test_the_cli_holds_no_push_procedure():
    """The click command renders `push_avatar`; the flags, the record and the
    platform calls have ONE home."""
    import inspect
    from cli.commands import avatar as cli_avatar
    src = inspect.getsource(cli_avatar)
    for gone in ("bool_env", "pushed.json", "push_twitter", "push_discord",
                 "push_telegram", "_record_pushed", "set_avatar_from_file"):
        assert gone not in src
