import types
from unittest.mock import AsyncMock

import pytest

from core.billing_context import mark_billed, reset_billed, mark_prepaid, reset_prepaid
from modules.llm.billing_guard import require_billable_model


@pytest.mark.parametrize("prepaid", [False, True])
def test_unknown_metered_model_refused(prepaid):
    token = mark_prepaid() if prepaid else mark_billed()
    try:
        with pytest.raises(ValueError, match="explicitly priced"):
            require_billable_model("totally-unpriced-model", "openai")
    finally:
        (reset_prepaid if prepaid else reset_billed)(token)


def test_unknown_local_operator_model_unchanged():
    require_billable_model("totally-unpriced-model", "openai")


def test_priced_and_flat_rate_models_are_allowed():
    from modules.llm.model_registry import get_all_models
    model = next(m for m in get_all_models() if m.pricing)
    token = mark_billed()
    try:
        require_billable_model(model.name, "openai")
        require_billable_model("custom-model", "zai-coding")
    finally:
        reset_billed(token)


@pytest.mark.asyncio
async def test_billing_context_reaches_child_tasks():
    import asyncio
    async def call():
        require_billable_model("totally-unpriced-model", "openai")
    token = mark_billed()
    try:
        task = asyncio.create_task(call())
    finally:
        reset_billed(token)
    with pytest.raises(ValueError, match="explicitly priced"):
        await task


@pytest.mark.asyncio
async def test_existing_adapter_refuses_before_provider_invocation(monkeypatch):
    from modules.llm.adapters import LLMClientAdapter
    from modules.llm.messages import HumanMessage
    adapter = object.__new__(LLMClientAdapter)
    monkeypatch.setattr("modules.llm.aux_metering._llm_identity", lambda _: ("totally-unpriced-model", "openai"))
    token = mark_billed()
    try:
        with pytest.raises(ValueError, match="explicitly priced"):
            await adapter._agenerate([HumanMessage(content="run")])
        with pytest.raises(ValueError, match="explicitly priced"):
            async for _ in adapter._astream_true([HumanMessage(content="run")]):
                pass
    finally:
        reset_billed(token)
