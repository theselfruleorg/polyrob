"""Proposal 125 S34: two environment predicates, one home (core.env).

``is_production_env`` drives every refusal (default False); ``is_development_env``
drives every relaxation and is True only for an EXPLICIT development name that no
other name contradicts (default False). A box with no name is neither.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from core.env import is_development_env, is_production_env

_ENV_NAMES = ("ENVIRONMENT", "POLYROB_ENV", "CONFIG_ENV", "ENV", "PRODUCTION",
              "MCP_ENCRYPTION_KEY")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in _ENV_NAMES:
        monkeypatch.delenv(n, raising=False)


def test_no_name_is_neither_production_nor_development():
    assert not is_production_env()
    assert not is_development_env()


@pytest.mark.parametrize("name,value", [
    ("ENVIRONMENT", "development"), ("POLYROB_ENV", "dev"),
    ("CONFIG_ENV", "development"), ("ENV", "Development")])
def test_an_explicit_development_name_counts(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert is_development_env()


def test_production_wins_over_a_development_name(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POLYROB_ENV", "development")
    assert not is_development_env()


def test_production_bool_wins_over_a_development_name(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("PRODUCTION", "true")
    assert not is_development_env()


def test_resolved_name_counts():
    assert is_development_env("development")
    assert not is_development_env("production")


@pytest.mark.parametrize("name,value", [
    ("PRODUCTION", "true"), ("CONFIG_ENV", "production"), ("ENV", "prod"),
    ("ENVIRONMENT", "production"), ("POLYROB_ENV", "prod")])
def test_mcp_key_fails_hard_on_every_production_name(monkeypatch, name, value):
    from core.security import encryption
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="MCP_ENCRYPTION_KEY"):
        encryption._load_or_generate_key()


def test_mcp_alias_is_the_core_predicate():
    import tools.mcp.security as mcp_security
    assert mcp_security._is_production is is_production_env


@pytest.mark.asyncio
async def test_development_name_does_not_skip_the_tier_gate(monkeypatch):
    from api.payment_verification import verify_payment_for_request
    from core.container import DependencyContainer
    from fastapi import HTTPException
    request = Request({"type": "http", "path": "/task/sessions", "headers": []})
    request.state.user_id, request.state.role, request.state.tier = "u1", "user", "free"
    tier = SimpleNamespace(get_user_tier=AsyncMock(return_value="free"))
    balance = SimpleNamespace(has_sufficient_balance=AsyncMock(return_value=True))
    services = {"tier_manager": tier, "balance_manager": balance}
    container = SimpleNamespace(config=None, get_service=services.get)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: container)
    with pytest.raises(HTTPException) as error:
        await verify_payment_for_request(request)
    assert error.value.status_code == 403


def test_loop_detection_has_one_threshold_pair(monkeypatch):
    from agents.task.constants import LoopDetectionConfig
    assert not hasattr(LoopDetectionConfig, "IS_DEVELOPMENT")
    import os
    if os.getenv("MAX_REPETITIONS") is None:
        assert LoopDetectionConfig.MAX_REPETITIONS == 3
    if os.getenv("UNCHANGED_STATE_THRESHOLD") is None:
        assert LoopDetectionConfig.UNCHANGED_STATE_THRESHOLD == 4


def test_server_config_has_no_environment_field():
    from core.config import ServerConfig
    assert "environment" not in ServerConfig.model_fields
    assert not hasattr(ServerConfig, "is_development")
