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
