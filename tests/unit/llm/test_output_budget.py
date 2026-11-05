"""057 WS-B: the per-session output budget and the truncation signal."""
import types

import pytest

from agents.task.session_class import apply_session_output_budget
from modules.llm.output_budget import (
    apply_output_budget, finish_reason_of, last_output_tokens,
    output_token_cap, output_was_truncated,
)
from core.autonomy_marker import mark_autonomous


class _Client:
    def __init__(self, response=None):
        self.last_response = response


class _Llm:
    def __init__(self, client):
        self._client = client


def _openai_response(reason, completion_tokens=None):
    usage = types.SimpleNamespace(completion_tokens=completion_tokens)
    choice = types.SimpleNamespace(finish_reason=reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    apply_output_budget(0)
    yield
    apply_output_budget(0)


def test_cap_is_the_existing_ceiling_by_default():
    assert output_token_cap(_Client(), 16384) == 16384


def test_env_ceiling_still_wins(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    assert output_token_cap(_Client(), 16384) == 4096


def test_budget_is_off_without_the_flag():
    mark_autonomous("ob-goal-1", "g1")
    llm = _Llm(_Client())
    assert apply_session_output_budget("ob-goal-1") == 0
    assert output_token_cap(llm._client, 16384) == 16384


def test_autonomous_session_is_narrowed(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    mark_autonomous("ob-goal-2", "g2")
    llm = _Llm(_Client())
    assert apply_session_output_budget("ob-goal-2") == 8192
    assert output_token_cap(llm._client, 16384) == 8192


def test_an_owner_chat_is_not_narrowed(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    llm = _Llm(_Client())
    assert apply_session_output_budget("ob-chat-1") == 0
    assert output_token_cap(llm._client, 16384) == 16384


def test_the_budget_never_raises_a_lower_ceiling(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2048")
    mark_autonomous("ob-goal-3", "g3")
    llm = _Llm(_Client())
    apply_session_output_budget("ob-goal-3")
    assert output_token_cap(llm._client, 16384) == 2048


def test_finish_reason_openai_shape():
    c = _Client(_openai_response("length", 16384))
    assert finish_reason_of(c) == "length"
    assert output_was_truncated(_Llm(c)) is True
    assert last_output_tokens(_Llm(c)) == 16384


def test_finish_reason_dict_shape():
    c = _Client({"choices": [{"finish_reason": "length"}],
                 "usage": {"completion_tokens": 4096}})
    assert finish_reason_of(c) == "length"
    assert output_was_truncated(_Llm(c)) is True
    assert last_output_tokens(_Llm(c)) == 4096


def test_finish_reason_anthropic_shape():
    c = _Client(types.SimpleNamespace(stop_reason="max_tokens"))
    assert finish_reason_of(c) == "max_tokens"
    assert output_was_truncated(_Llm(c)) is True


def test_a_clean_stop_is_not_truncation():
    assert output_was_truncated(_Llm(_Client(_openai_response("stop", 120)))) is False
    assert output_was_truncated(_Llm(_Client(_openai_response("tool_calls")))) is False


def test_unreadable_response_is_unknown_never_a_confident_no():
    assert finish_reason_of(_Client(None)) is None
    assert finish_reason_of(_Client(types.SimpleNamespace())) is None
    # unknown must not be reported as truncated either
    assert output_was_truncated(_Llm(_Client(None))) is False


def test_modules_tier_cap_is_session_agnostic():
    """The layering half: modules/llm holds the cap, agents decides who gets one."""
    assert apply_output_budget(4096) == 4096
    assert output_token_cap(_Client(), 16384) == 4096
    assert apply_output_budget(0) == 0
    assert output_token_cap(_Client(), 16384) == 16384


def test_a_cron_cap_does_not_reach_an_owner_chat_on_the_shared_client(monkeypatch):
    """Prod 2026-11-04: a cron run's stamp on the shared client cut the owner's
    compaction summary at 8,192."""
    import asyncio
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    mark_autonomous("ob-goal-4", "g4")
    shared = _Client()
    seen = {}
    cron_set, chat_read = asyncio.Event(), asyncio.Event()

    async def _cron():
        apply_session_output_budget("ob-goal-4")
        cron_set.set()
        await chat_read.wait()
        seen["cron"] = output_token_cap(shared, 16384)

    async def _chat():
        await cron_set.wait()
        apply_session_output_budget("ob-chat-2")
        seen["chat"] = output_token_cap(shared, 16384)
        chat_read.set()

    async def _main():
        await asyncio.gather(_cron(), _chat())

    asyncio.run(_main())
    assert seen == {"cron": 8192, "chat": 16384}


def test_a_chat_spawned_from_a_cron_task_clears_the_inherited_cap(monkeypatch):
    import asyncio
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    mark_autonomous("ob-goal-5", "g5")

    async def _child():
        apply_session_output_budget("ob-chat-3")
        return output_token_cap(_Client(), 16384)

    async def _parent():
        apply_session_output_budget("ob-goal-5")
        return await asyncio.create_task(_child())

    assert asyncio.run(_parent()) == 16384


def test_bounded_output_is_per_task_and_never_stamps_the_shared_client(monkeypatch):
    """095 W1d: the last-report bound narrows only the calls inside its scope; a
    concurrent task on the same client keeps the normal ceiling."""
    import asyncio
    from types import SimpleNamespace
    from modules.llm.output_budget import bounded_output, output_token_cap
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    client = SimpleNamespace()
    seen = {}
    inside, release = asyncio.Event(), asyncio.Event()

    async def _bounded():
        with bounded_output(1500):
            seen["bounded"] = output_token_cap(client, 16384)
            inside.set()
            await release.wait()

    async def _other():
        await inside.wait()
        seen["other"] = output_token_cap(client, 16384)
        release.set()

    async def _main():
        await asyncio.gather(_bounded(), _other())

    asyncio.run(_main())
    assert seen == {"bounded": 1500, "other": 16384}
    assert not hasattr(client, "_polyrob_session_output_cap")
    assert output_token_cap(client, 16384) == 16384
