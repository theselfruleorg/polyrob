"""Model-identity SSOT: the model/provider the agent ACTUALLY runs on is pinned as a
frozen foundation message (like self-context), so the agent answers "what model are
you" from an authoritative line instead of grepping config/.env (which leaked
secrets). Refreshed on a live /model swap.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin, make_control_message


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


def test_runtime_identity_injected_as_foundation_not_system_prompt():
    mm = _mm()
    mm.set_runtime_identity("z-ai/glm-5.2", "openrouter")
    msgs = mm.get_messages_for_llm()
    # Not baked into the (cacheable) system prompt.
    assert "z-ai/glm-5.2" not in _text(msgs[0])
    blocks = [m for m in msgs if "runtime-identity" in _text(m)]
    assert len(blocks) == 1
    assert "z-ai/glm-5.2" in _text(blocks[0])
    assert "openrouter" in _text(blocks[0])
    assert blocks[0].to_dict()["role"] == "user"


def test_runtime_identity_is_first_after_system_prompt():
    mm = _mm()
    mm.set_runtime_identity("m", "p")
    mm.set_self_context_message("SOUL")
    msgs = mm.get_messages_for_llm()
    joined = [_text(m) for m in msgs]
    ident_idx = next(i for i, t in enumerate(joined) if "runtime-identity" in t)
    soul_idx = next(i for i, t in enumerate(joined) if "SOUL" in t)
    assert ident_idx == 1  # right after the system message at index 0
    assert ident_idx < soul_idx


def test_runtime_identity_origin_and_envelope():
    m = make_control_message("id", MessageOrigin.RUNTIME_IDENTITY)
    assert m.origin == MessageOrigin.RUNTIME_IDENTITY
    assert "<runtime-identity>" in m.content


def test_no_identity_block_when_unset():
    mm = _mm()
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "runtime-identity" in _text(m)]


def test_falsy_model_is_noop():
    mm = _mm()
    mm.set_runtime_identity("", "openrouter")
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "runtime-identity" in _text(m)]


def test_unknown_provider_labeled_not_none():
    mm = _mm()
    mm.set_runtime_identity("some-model", None)
    blocks = [m for m in mm.get_messages_for_llm() if "runtime-identity" in _text(m)]
    assert len(blocks) == 1
    assert "unknown" in _text(blocks[0])
    assert "None" not in _text(blocks[0])


def test_identity_refresh_replaces_prior():
    mm = _mm()
    mm.set_runtime_identity("old-model", "openrouter")
    mm.set_runtime_identity("new-model", "anthropic")  # simulates a swap refresh
    joined = " ".join(_text(m) for m in mm.get_messages_for_llm())
    assert "new-model" in joined and "anthropic" in joined
    assert "old-model" not in joined


def test_identity_tokens_counted():
    mm = _mm()
    mm.set_runtime_identity("z-ai/glm-5.2", "openrouter")
    assert getattr(mm, "_runtime_identity_tokens", 0) > 0


def test_identity_tokens_included_in_totals():
    """Parity with self/project context: the identity block's tokens must be summed
    into the overflow/compaction accounting, not silently dropped."""
    mm = _mm()
    before_total = mm.get_total_tokens()
    before_count = mm.get_token_count()
    mm.set_runtime_identity("z-ai/glm-5.2", "openrouter")
    assert mm.get_total_tokens() == before_total + mm._runtime_identity_tokens
    assert mm.get_token_count() == before_count + mm._runtime_identity_tokens
