"""polyrob x accepts user context, never an app-only bearer token."""
import pytest

from cli.commands.x import XCredentialsError, check_x_credentials


_ALL = (
    "TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET", "TWITTER_BEARER_TOKEN",
    "TWITTER_OAUTH2_ACCESS_TOKEN",
)


def _clear(monkeypatch):
    for name in _ALL:
        monkeypatch.delenv(name, raising=False)


def test_oauth2_user_token_is_sufficient(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "user-token")
    check_x_credentials()


def test_app_bearer_token_is_not_user_context(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "app-only")
    with pytest.raises(XCredentialsError, match="app-only"):
        check_x_credentials()


def test_complete_oauth1_user_credentials_remain_supported(monkeypatch):
    _clear(monkeypatch)
    for name in _ALL[:4]:
        monkeypatch.setenv(name, "set")
    check_x_credentials()
