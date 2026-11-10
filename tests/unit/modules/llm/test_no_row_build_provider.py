"""A model with no row and no family builds on the provider its id names (106)."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin
from agents.task.config import resolve_session_provider_model
from modules.llm.model_registry import get_model_config
from modules.llm.usage_extract import detect_llm_provider


class _Builder(LLMProvisioningMixin):
    def __init__(self, get_chat_model):
        self.logger = logging.getLogger("t.no_row")
        self.container = SimpleNamespace(
            get_service=lambda name: SimpleNamespace(get_chat_model=get_chat_model))


def test_a_vendor_slug_with_no_row_names_openrouter():
    assert get_model_config("acme/no-row").provider.value == "openrouter"
    assert detect_llm_provider("acme/no-row") == "openrouter"
    assert resolve_session_provider_model(None, "acme/no-row") == ("openrouter", "acme/no-row")


def test_a_bare_id_with_no_row_names_openai():
    assert get_model_config("no-row-bare").provider.value == "openai"


def test_a_family_keeps_its_own_row():
    assert get_model_config("acme/claude-sonnet-99").provider.value == "anthropic"


def test_the_build_goes_to_openrouter_when_that_client_exists():
    get_chat_model = AsyncMock(return_value="model")
    built = asyncio.run(_Builder(get_chat_model)._create_llm_from_config_async(
        {"model": "acme/no-row"}, isolated=True))
    assert built == "model"
    assert get_chat_model.await_args.kwargs["provider"] == "openrouter"


def test_with_no_openrouter_client_the_build_fails_and_sends_nothing_to_another_provider():
    get_chat_model = AsyncMock(side_effect=ValueError("LLM client for provider 'openrouter' is not available."))
    built = asyncio.run(_Builder(get_chat_model)._create_llm_from_config_async(
        {"model": "acme/no-row"}, isolated=True))
    assert built is None
    assert get_chat_model.await_count == 1
    assert get_chat_model.await_args.kwargs["provider"] == "openrouter"
