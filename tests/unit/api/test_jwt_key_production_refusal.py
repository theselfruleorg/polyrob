"""OPS-5: the API boot refusal sees prod's ENVIRONMENT=production.

The check used to read only CONFIG_ENV/ENV (default "development"), so on prod
a short JWT secret only warned, and a template placeholder passed silently.
"""
import pytest

from api.app import create_app, jwt_secret_weakness
from core.env import is_production_env

_ENV_NAMES = ("ENVIRONMENT", "POLYROB_ENV", "CONFIG_ENV", "ENV", "PRODUCTION")
STRONG = "q8N2vR7xLp4Tz9Kc1Wm6Hs3Yd5Bf0Jg2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in _ENV_NAMES:
        monkeypatch.delenv(n, raising=False)


@pytest.mark.parametrize("name,value", [
    ("ENVIRONMENT", "production"), ("POLYROB_ENV", "prod"),
    ("CONFIG_ENV", "production"), ("PRODUCTION", "true")])
def test_every_production_name_counts(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert is_production_env()


def test_development_is_not_production():
    assert not is_production_env("development")


@pytest.mark.parametrize("secret", [
    "short", "REPLACE_WITH_SECURE_KEY_0000000000000000",
    "your-secret-key-here-change-me-please-now", "a" * 40,
    "change-me-in-production-1234567890abcdef"])
def test_weak_and_placeholder_secrets_are_named(secret):
    assert jwt_secret_weakness(secret)


def test_a_random_secret_is_fit():
    assert jwt_secret_weakness(STRONG) is None


def test_prod_environment_refuses_a_short_secret(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="refused in production"):
        create_app()


def test_prod_environment_refuses_a_placeholder(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "REPLACE_WITH_SECURE_KEY_0123456789abcdef")
    with pytest.raises(RuntimeError, match="placeholder"):
        create_app()


def test_development_only_warns(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")
    assert create_app() is not None
