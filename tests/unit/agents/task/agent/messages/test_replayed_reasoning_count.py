"""Replayed reasoning is a prompt byte: the local count and the warm check read it."""
import logging
from types import SimpleNamespace

from agents.task.agent.messages.compactor import _SUMMARIZE_INPUT_BUDGET_CHARS, CompactorMixin
from agents.task.agent.messages.token_counter import TokenCounterMixin
from modules.llm.messages import AIMessage, HumanMessage


class _Counter(TokenCounterMixin):
    def __init__(self):
        self.logger = logging.getLogger("t.reasoning")
        self.model_name = "deepseek/deepseek-v4-pro"


def _replayed(text):
    message = AIMessage(content="answer")
    message.additional_kwargs.update(reasoning_content=text, reasoning_seat="deepseek-native")
    return message


def test_the_local_count_holds_the_replayed_reasoning():
    counter = _Counter()
    assert counter._count_message_tokens(_replayed("word " * 400)) > \
        counter._count_message_tokens(AIMessage(content="answer")) + 300


def test_the_fallback_count_holds_the_replayed_reasoning(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no tokenizer")
    monkeypatch.setattr("agents.task.agent.messages.token_counter.count_messages_tokens", broken)
    assert _Counter()._count_message_tokens(_replayed("x" * 4000)) >= 1000


class _Warm(CompactorMixin):
    def __init__(self, llm):
        self.llm = llm
        self.use_native_tools = False


def test_the_warm_check_counts_the_replayed_reasoning():
    llm = SimpleNamespace()
    warm = _Warm(llm)
    half = "x" * (_SUMMARIZE_INPUT_BUDGET_CHARS // 2)
    assert warm._warm_summarization_available(llm, [HumanMessage(content=half)]) is True
    assert warm._warm_summarization_available(llm, [_replayed(half + half)]) is False
