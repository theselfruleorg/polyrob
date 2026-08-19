"""P3+P4+P7 (context-usage audit 2026-08-15) — the ctx gauge must count what is
actually sent.

Measured on a fresh local session: the environment block (261 tok) and the
tool-catalog block (770 tok) were in get_messages_for_llm but in NO gauge sum;
tool schemas (7,512 tok on the default local rig) were invisible; and the H-MEM
words*1.3 estimate measured only 0.58-0.77 of the real counter.
"""
from unittest.mock import MagicMock

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.message_manager.config import MessageManagerConfig
from agents.task.agent.prompts import SystemPrompt


def _mm():
    mm = MessageManager.from_config(
        llm=MagicMock(), task="t", action_descriptions="a",
        system_prompt_class=SystemPrompt, config=MessageManagerConfig(),
    )
    # Deterministic token counting: use a registry-known model name.
    mm._model_name = "gpt-4o"
    return mm


# --- P3: environment + tool-catalog blocks are counted -----------------------

def test_environment_block_is_counted():
    mm = _mm()
    base = mm.get_token_count()
    mm.set_environment_message("env " * 200)
    tracked = mm._environment_tokens
    assert tracked > 0
    assert mm.get_token_count() == base + tracked
    assert mm.get_total_tokens() >= base + tracked


def test_tool_catalog_block_is_counted():
    mm = _mm()
    base = mm.get_token_count()
    mm.set_tool_catalog_message("<tool-catalog>" + "row " * 300 + "</tool-catalog>")
    tracked = mm._tool_catalog_tokens
    assert tracked > 0
    assert mm.get_token_count() == base + tracked


def test_token_stats_base_includes_new_blocks():
    mm = _mm()
    mm.set_environment_message("env " * 100)
    mm.set_tool_catalog_message("cat " * 100)
    stats = mm.get_token_stats()
    assert stats['base'] == mm.get_token_count()


# --- P4: tool-schema tokens (the `tools` param) ------------------------------

def test_tool_schema_tokens_included_by_default(monkeypatch):
    monkeypatch.delenv("CTX_COUNT_TOOL_SCHEMAS", raising=False)
    mm = _mm()
    base = mm.get_actual_token_count()
    mm.set_tool_schema_tokens(1234)
    assert mm.get_actual_token_count() == base + 1234


def test_tool_schema_tokens_flag_off_restores_legacy(monkeypatch):
    monkeypatch.setenv("CTX_COUNT_TOOL_SCHEMAS", "false")
    mm = _mm()
    base = mm.get_actual_token_count()
    mm.set_tool_schema_tokens(1234)
    assert mm.get_actual_token_count() == base


def test_tool_schema_tokens_never_negative_or_stale():
    mm = _mm()
    mm.set_tool_schema_tokens(500)
    mm.set_tool_schema_tokens(0)  # re-stamp resets
    assert mm.get_actual_token_count() == mm.get_token_count()


# --- P7: H-MEM real count replaces words*1.3 ---------------------------------

class _Tcm:
    """Fake TaskContextManager returning a fixed context injection."""

    def __init__(self, context):
        self._context = context

    def get_context_injection(self, session_id):
        return self._context


def test_hmem_uses_real_counter_not_word_estimate():
    # One "word" that is many tokens: the words*1.3 estimate would say ~1 token,
    # the real counter says dozens. The gauge must reflect the real count.
    context = ",".join(f"finding{i}" for i in range(120))  # zero spaces
    mm = _mm()
    mm.session_id = "s1"
    mm.task_context_manager = _Tcm(context)

    from modules.llm import count_tokens
    real = count_tokens(context, "gpt-4o")
    word_estimate = int(len(context.split()) * 1.3)  # == 1
    assert word_estimate < real / 10  # precondition: the two are far apart

    delta = mm.get_actual_token_count() - mm.get_token_count()
    assert delta == real


def test_hmem_empty_context_adds_nothing():
    mm = _mm()
    mm.session_id = "s1"
    mm.task_context_manager = _Tcm("")
    assert mm.get_actual_token_count() == mm.get_token_count()
