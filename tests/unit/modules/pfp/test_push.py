"""Surface push for the avatar (modules/pfp/push.py).

Twitter/X is the only LIVE push (v1.1 update_profile_image); Telegram is
assisted (BotFather-only, honestly stated). Live paths are mocked — no network.
"""
import pytest

from modules.pfp import push


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
    p.write_bytes(b"\x89PNG")

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
    p.write_bytes(b"\x89PNG")
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
