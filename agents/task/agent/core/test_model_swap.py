import asyncio
import logging
from types import SimpleNamespace

from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (ensure Agent class + descriptors import)
from agents.task.agent.core.model_swap import ModelSwapMixin
from agents.task.agent.service import Agent
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


class _Agent(ModelSwapMixin):
    """Stub agent whose ``model_name``/``provider_name`` DELEGATE to the message
    manager — mirroring the real ``Agent`` property descriptors (getter+setter),
    so a single write updates both the agent view and the MessageManager SSOT.
    (The old version made these plain instance attributes, which is exactly what
    masked the getter-only-property AttributeError in the real classes.)"""

    def __init__(self):
        self.logger = logging.getLogger("t")
        self.llm = SimpleNamespace()
        self.message_manager = SimpleNamespace(
            model_name="gpt-5", provider_name="openai", llm=self.llm
        )
        self.llm_provider = "openai"
        self.chat_model_library = "OpenAIAdapter"
        self._built = None

    @property
    def model_name(self):
        return self.message_manager.model_name

    @model_name.setter
    def model_name(self, value):
        self.message_manager.model_name = value

    @property
    def provider_name(self):
        return self.message_manager.provider_name

    @provider_name.setter
    def provider_name(self, value):
        self.message_manager.provider_name = value

    async def _create_llm_from_config_async(self, cfg, isolated=False):
        self._built = SimpleNamespace(kind="AnthropicAdapter", cfg=cfg)
        return self._built

    def _reconcile_native_tools(self, provider):
        self._reconciled = provider
        return True


def test_swap_updates_all_ssot():
    a = _Agent()
    r = asyncio.run(a.swap_model("anthropic", "claude-sonnet-4-5"))
    assert r["ok"] and r["previous"] == {"provider": "openai", "model": "gpt-5"}
    assert a.llm is a._built
    assert a.model_name == "claude-sonnet-4-5" and a.llm_provider == "anthropic"
    assert a.provider_name == "anthropic"
    assert a.message_manager.model_name == "claude-sonnet-4-5"
    assert a.message_manager.provider_name == "anthropic"
    assert a.message_manager.llm is a._built  # I3: compaction reads mm.llm
    assert a._reconciled == "anthropic"


def test_swap_failure_leaves_agent_unchanged():
    a = _Agent()

    async def _fail(cfg, isolated=False):
        return None

    a._create_llm_from_config_async = _fail
    r = asyncio.run(a.swap_model("bogus", "nope"))
    assert r["ok"] is False and a.model_name == "gpt-5" and a.llm_provider == "openai"


def test_swap_autodetects_provider_when_falsy():
    a = _Agent()
    r = asyncio.run(a.swap_model(None, "claude-sonnet-4-5"))
    assert r["ok"] is True
    assert a.llm_provider == "anthropic"          # derived from the registry for claude-*
    assert a.model_name == "claude-sonnet-4-5"


# --------------------------------------------------------------------------- #
# Real-classes integration test (Critical 1). Uses the REAL Agent property
# descriptors + a REAL MessageManager so the getter-only-property crash the
# task-level stubs masked is actually exercised. Pre-fix these raise
# AttributeError ("property 'provider_name' has no setter"); post-fix they pass.
# --------------------------------------------------------------------------- #

def _real_mm(model="gpt-4o", max_input=4000):
    llm = MagicMock()
    llm.model_name = model
    return MessageManager(
        llm=llm, task="t", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=max_input,
    )


def _real_agent(mm, build_result):
    a = Agent.__new__(Agent)  # live property descriptors, no full __init__
    a.logger = logging.getLogger("swap-real")
    a.message_manager = mm
    a.llm = mm.llm
    a.llm_provider = mm.provider_name
    a.chat_model_library = type(mm.llm).__name__
    a._reconciled = None

    async def _build(cfg, isolated=False):
        return build_result

    a._create_llm_from_config_async = _build

    def _recon(p):
        a._reconciled = p
        return True

    a._reconcile_native_tools = _recon
    return a


def test_real_agent_swap_updates_all_ssot_via_property_descriptors():
    mm = _real_mm("gpt-4o")
    built = MagicMock()
    built.model_name = "claude-sonnet-4-5"
    a = _real_agent(mm, built)

    r = asyncio.run(a.swap_model("anthropic", "claude-sonnet-4-5"))
    assert r["ok"] is True

    # These property reads/writes are what raised AttributeError pre-fix.
    assert a.provider_name == "anthropic"
    assert a.model_name == "claude-sonnet-4-5"
    assert mm.model_name == "claude-sonnet-4-5"
    assert mm.provider_name == "anthropic"
    assert mm.llm is built            # I3: post-swap compaction runs on the new llm
    assert a.llm is built
    assert a.llm_provider == "anthropic"
    assert a._reconciled == "anthropic"


def test_real_agent_swap_recomputes_token_budgets(monkeypatch):
    # I4: budgets are init-time; a swap to a different context window must
    # recompute them (reusing the init formula, not the raw window).
    monkeypatch.delenv("TASK_MAX_INPUT_TOKENS", raising=False)
    mm = _real_mm("gpt-4o", max_input=4000)
    assert mm.max_input_tokens == 4000
    built = MagicMock()
    built.model_name = "claude-sonnet-4-5"
    a = _real_agent(mm, built)

    asyncio.run(a.swap_model("anthropic", "claude-sonnet-4-5"))
    # Recomputed from the new model's registry context window (not the 4000 override).
    assert mm.max_input_tokens != 4000
    assert mm.max_input_tokens > 4000
    assert mm.safe_input_tokens < mm.max_input_tokens


def test_real_agent_swap_failure_changes_nothing():
    mm = _real_mm("gpt-4o")
    original_llm = mm.llm
    a = _real_agent(mm, None)  # build returns None -> failure

    r = asyncio.run(a.swap_model("anthropic", "claude-sonnet-4-5"))
    assert r["ok"] is False
    assert a.model_name == "gpt-4o"
    assert a.llm is original_llm
    assert mm.llm is original_llm
    assert a.llm_provider == "openai"


def test_real_agent_swap_completes_when_reconcile_raises():
    mm = _real_mm("gpt-4o")
    built = MagicMock()
    built.model_name = "claude-sonnet-4-5"
    a = _real_agent(mm, built)

    def _boom(p):
        raise RuntimeError("reconcile boom")

    a._reconcile_native_tools = _boom
    r = asyncio.run(a.swap_model("anthropic", "claude-sonnet-4-5"))
    # Reconcile is best-effort; the swap still completes and SSOT is updated.
    assert r["ok"] is True
    assert a.model_name == "claude-sonnet-4-5"
    assert mm.llm is built
