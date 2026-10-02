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
