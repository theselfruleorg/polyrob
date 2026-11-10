import pytest

from core.surfaces.telegram_api import PUBLIC_BOT_API, local_bot_api_url, telegram_api_base


def test_unset_is_the_public_server():
    assert local_bot_api_url({}) is None
    assert telegram_api_base({}) == PUBLIC_BOT_API


def test_a_loopback_url_is_the_local_server():
    env = {"TELEGRAM_BOT_API_URL": "http://127.0.0.1:8081/"}
    assert telegram_api_base(env) == "http://127.0.0.1:8081"


@pytest.mark.parametrize("url", ["http://example.com:8081", "file:///x", "127.0.0.1:8081"])
def test_another_host_is_refused(url):
    assert local_bot_api_url({"TELEGRAM_BOT_API_URL": url}) is None
