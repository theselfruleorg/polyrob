"""094 — the hot model switch: grammar, aliases, pins, the chat tier, the live
catalog, the price guard, the queued swap, reasoning per session, the delegate
allowlist and the owner-only job model."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from core import model_switch as ms
from core.price_guard import guard_reason

PIN_ENV = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash"}


# --- grammar ------------------------------------------------------------------- #

@pytest.mark.parametrize("words,action,extra", [
    ([], "show", {}),
    (["sonnet"], "set", {"name": "sonnet", "default": False}),
    (["sonnet", "default"], "set", {"name": "sonnet", "default": True}),
    (["sonnet", "reasoning=high"], "set", {"reasoning": "high"}),
    (["sonnet", "go"], "set", {"name": "sonnet"}),      # an old confirm word is ignored
    (["openrouter", "z-ai/glm-5.3"], "set", {"provider": "openrouter", "name": "z-ai/glm-5.3"}),
    (["reasoning=off"], "reasoning", {"reasoning": "off"}),
    (["reasoning=2048", "default"], "reasoning", {"reasoning": "2048", "default": True}),
    (["reset"], "reset", {}),
    (["default"], "set", {"name": "default", "default": False}),
    (["aliases"], "aliases", {}),
    (["alias", "fav", "openrouter/x/y"], "alias", {"name": "fav", "alias_target": "openrouter/x/y"}),
])
def test_parse(words, action, extra):
    cmd = ms.parse_model_args(words)
    assert cmd.action == action, cmd.error
    for key, value in extra.items():
        assert getattr(cmd, key) == value


@pytest.mark.parametrize("words", [["sonnet", "reasoning=extreme"], ["a", "b", "c"],
                                   ["alias", "sonnet", "x/y"], ["reset", "now"]])
def test_parse_refuses(words):
    assert ms.parse_model_args(words).action == "error"


# --- aliases ------------------------------------------------------------------------- #

KNOWN = ("openrouter", "anthropic", "openai", "deepseek", "gemini")


def test_alias_prefers_the_operator_provider():
    r = ms.resolve_name("sonnet", operator_provider="openrouter",
                        keyed_providers=["openrouter", "anthropic"], known_providers=KNOWN)
    assert (r.provider, r.model) == ("openrouter", "anthropic/claude-sonnet-5.5")


def test_alias_falls_to_a_keyed_native_provider():
    r = ms.resolve_name("sonnet", operator_provider="deepseek",
                        keyed_providers=["deepseek", "anthropic"], known_providers=KNOWN)
    assert (r.provider, r.model) == ("anthropic", "claude-sonnet-5-5")


def test_alias_with_no_serving_key_is_refused_not_guessed():
    r = ms.resolve_name("glm", operator_provider="anthropic", keyed_providers=["anthropic"],
                        known_providers=KNOWN)
    assert r.error and "openrouter" in r.error


def test_every_major_family_has_an_alias():
    for name in ("deepseek", "deepseek-pro", "glm", "glm-flash", "opus", "sonnet", "haiku",
                 "gpt", "gemini", "grok", "kimi", "qwen", "minimax", "mistral", "llama"):
        assert "openrouter" in ms.alias_row(name)


def test_owner_alias_wins_and_symbolic_aliases_read_the_env():
    r = ms.resolve_name("fav", operator_provider="openrouter", known_providers=KNOWN,
                        owner_aliases={"fav": "anthropic/claude-opus-5-5"})
    assert (r.provider, r.model) == ("anthropic", "claude-opus-5-5")
    r = ms.resolve_name("default", operator_provider="openrouter", known_providers=KNOWN,
                        env=PIN_ENV)
    assert r.model == "deepseek/deepseek-v4.1-flash" and r.via == "the default model"
    assert "HEAVY_MODEL" in ms.resolve_name("heavy", operator_provider="openrouter",
                                            known_providers=KNOWN, env=PIN_ENV).error


def test_ids_and_provider_prefix():
    r = ms.resolve_name("openrouter/anthropic/claude-sonnet-5.5", known_providers=KNOWN)
    assert (r.provider, r.model) == ("openrouter", "anthropic/claude-sonnet-5.5")
    r = ms.resolve_name("x-ai/grok-4.7", operator_provider="openrouter", known_providers=KNOWN)
    assert (r.provider, r.model) == ("openrouter", "x-ai/grok-4.7")


def test_guard():
    flash, sonnet = (0.045, 0.5), (2.0, 10.0)
    assert "input price" in guard_reason(flash, sonnet)
    assert guard_reason(sonnet, flash) == ""
    assert "per million output" in guard_reason(None, (5.0, 31.0))
    assert guard_reason(flash, None) == ""


# --- the owner store and the chat tier -------------------------------------------- #

@pytest.fixture
def home(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path))
    for k, v in PIN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("HEAVY_MODEL", raising=False)
    return tmp_path


def test_pins_roundtrip_and_unreadable_is_not_empty(home):
    from core import model_pins
    assert model_pins.load("owner1") == ({}, "")
    assert model_pins.set_default("owner1", "openrouter", "z-ai/glm-5.3", "high")[0]
    assert model_pins.owner_default("owner1")["reasoning"] == "high"
    assert model_pins.set_alias("owner1", "fav", "openrouter/x/y")[0]
    assert model_pins.aliases("owner1") == {"fav": "openrouter/x/y"}
    path = next(home.rglob("model.json"))
    path.write_text("{not json")
    data, err = model_pins.load("owner1")
    assert data == {} and "could not be read" in err
    assert not model_pins.set_alias("owner1", "x", "y/z")[0]   # never overwrite unreadable


def test_chat_tier_precedence(home):
    from core import model_pins
    from core.model_pins import chat_runtime_for
    provider, model, _r, source = chat_runtime_for("owner1")
    assert (provider, model) == ("openrouter", "deepseek/deepseek-v4.1-flash")
    assert "DEFAULT_MODEL" in source
    model_pins.set_default("owner1", "openrouter", "z-ai/glm-5.3")
    provider, model, _r, source = chat_runtime_for("owner1")
    assert model == "z-ai/glm-5.3" and "your default" in source


# --- reasoning per session ------------------------------------------------------- #

def test_session_reasoning_scope_wins_over_env_and_ends(monkeypatch):
    from modules.llm import openrouter_reasoning as orr
    from modules.llm.call_scope import session_reasoning
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    assert orr.reasoning_extra_body(8000) == {"reasoning": {"enabled": False}}
    with session_reasoning("high"):
        assert orr.reasoning_extra_body(8000) == {"reasoning": {"effort": "high"}}
    with session_reasoning("64000"):
        assert orr.reasoning_extra_body(8000) == {"reasoning": {"max_tokens": 6000}}
    assert orr.reasoning_extra_body(8000) == {"reasoning": {"enabled": False}}


def test_runner_reads_the_session_level(monkeypatch):
    from agents.task.agent.core import llm_runner
    from agents.task.model_pin_state import set_session_reasoning
    from modules.llm import openrouter_reasoning as orr
    seen = {}

    class _A(llm_runner.LLMRunnerMixin if hasattr(llm_runner, "LLMRunnerMixin") else object):
        session_id = "s-1"

        async def _get_next_action_unscoped(self, msgs):
            seen["body"] = orr.reasoning_extra_body(1000)
            return "ok"

    set_session_reasoning("s-1", "low")
    cls = next(v for v in vars(llm_runner).values()
               if isinstance(v, type) and "get_next_action" in vars(v))
    agent = _A()
    assert asyncio.run(cls.get_next_action.__wrapped__(agent, [])
                       if hasattr(cls.get_next_action, "__wrapped__")
                       else cls.get_next_action(agent, [])) == "ok"
    assert seen["body"] == {"reasoning": {"effort": "low"}}


# --- the live catalog ---------------------------------------------------------------- #

CATALOG = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash", "context_length": 1048576,
     "pricing": {"prompt": "0.000000045", "completion": "0.0000005", "input_cache_read": "0.000000006"},
     "supported_parameters": ["tools", "reasoning"]},
    {"id": "anthropic/claude-sonnet-5.5", "context_length": 1000000,
     "pricing": {"prompt": "0.000002", "completion": "0.00001"},
     "supported_parameters": ["tools"]},
    {"id": "anthropic/claude-haiku-5.5", "context_length": 1000000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
     "supported_parameters": ["tools", "reasoning"]},
    {"id": "meta-llama/llama-4-maverick", "context_length": 1000000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
     "supported_parameters": ["tools"]},
    {"id": "z-ai/glm-5.3", "context_length": 1048576,
     "pricing": {"prompt": "0.00000007", "completion": "0.0000014"},
     "supported_parameters": ["tools"]},
    {"id": "openai/gpt-image-x", "context_length": 8000,
     "pricing": {"prompt": "0.000001", "completion": "0.000001"},
     "supported_parameters": []},
]}


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    from modules.llm import openrouter_catalog as oc
    monkeypatch.setattr(oc, "_cache_path", lambda: tmp_path / "cache" / "or.json")
    monkeypatch.setattr(oc, "_MEM", {"at": 0.0, "rows": None})
    state = {"fail": False, "calls": 0}

    def fake_fetch():
        state["calls"] += 1
        if state["fail"]:
            raise OSError("network down")
        return CATALOG

    monkeypatch.setattr(oc, "_fetch", fake_fetch)
    # register_live_model writes the process-wide registry: restore it, so a
    # fake catalog row (e.g. an 8k-window image model) never leaks into a later
    # registry ratchet.
    from modules.llm.model_registry import get_registry
    reg = get_registry()
    monkeypatch.setattr(reg, "_models", dict(reg._models))
    monkeypatch.setattr(reg, "_aliases", dict(reg._aliases))
    return state


def test_catalog_lookup_cache_and_failure(catalog, monkeypatch):
    from modules.llm import openrouter_catalog as oc
    entry, read = oc.lookup("anthropic/claude-sonnet-5.5")
    assert read and entry.price == (2.0, 10.0) and entry.tools and entry.context == 1000000
    assert oc.lookup("no/such", refresh_on_miss=False) == (None, True)
    monkeypatch.setattr(oc, "_MEM", {"at": 0.0, "rows": None})
    catalog["fail"] = True
    assert oc.lookup("anthropic/claude-sonnet-5.5")[0] is not None   # disk cache answers
    (oc._cache_path()).unlink()
    monkeypatch.setattr(oc, "_MEM", {"at": 0.0, "rows": None})
    assert oc.lookup("anthropic/claude-sonnet-5.5") == (None, False)  # unreadable != missing


def test_register_live_model_fixes_the_window(catalog):
    from modules.llm import openrouter_catalog as oc
    from modules.llm.model_registry import get_model_config_exact
    entry, _ = oc.lookup("anthropic/claude-haiku-5.5")
    oc.register_live_model(entry)
    cfg = get_model_config_exact("anthropic/claude-haiku-5.5")
    assert cfg.context_window == 1000000 and cfg.pricing.output_price == 0.5


# --- the plan ----------------------------------------------------------------------- #

@pytest.fixture
def keyed(monkeypatch):
    import modules.llm.profiles as profiles
    monkeypatch.setattr(profiles, "providers_with_keys", lambda env=None: ["openrouter"])


def _plan(words, current=("openrouter", "deepseek/deepseek-v4.1-flash")):
    from agents.task.model_switch_service import plan_switch
    return asyncio.run(plan_switch(ms.parse_model_args(words), user_id="owner1", current=current))


def test_plan_has_no_price_gate(home, catalog, keyed):
    """121: the owner chooses a model knowingly; a pricier one switches at once."""
    plan, err = _plan(["sonnet"])
    assert plan and plan.model == "anthropic/claude-sonnet-5.5" and not hasattr(plan, "quote")
    plan, err = _plan(["sonnet", "go"])                   # an old confirm line still works
    assert plan and plan.model == "anthropic/claude-sonnet-5.5"


def test_plan_refuses_unknown_toolless_and_keyless(home, catalog, keyed):
    plan, err = _plan(["anthropic/claude-sonet-5.5"])
    assert plan is None and "Nearest: anthropic/claude-sonnet-5.5" in err
    plan, err = _plan(["openai/gpt-image-x"])
    assert plan is None and "cannot call tools" in err
    plan, err = _plan(["anthropic", "claude-sonnet-5-5"])
    assert plan is None and "No API key for anthropic" in err   # the native two-word form
    plan, err = _plan(["openai/gpt-5.5", "go"])
    assert plan is None and "not on openrouter" in err   # an OpenRouter id, not provider openai


def test_plan_unverified_when_the_list_cannot_be_read(home, catalog, keyed, monkeypatch):
    from modules.llm import openrouter_catalog as oc
    catalog["fail"] = True
    plan, err = _plan(["glm", "go"])
    assert plan and "could not be read" in plan.verified


# --- the verb on a chat seat -------------------------------------------------------- #

class _FakeAgent:
    def __init__(self):
        self.model_name, self.llm_provider, self.session_id = (
            "deepseek/deepseek-v4.1-flash", "openrouter", "s-1")
        self.pinned = None

    def request_model_pin(self, pin):
        self.pinned = pin

    def pending_model_pin(self):
        return self.pinned

    def cancel_model_pin(self):
        self.pinned = None


class _FakeSessions:
    def __init__(self):
        self.meta = {"s-1": {"request": {"provider": "openrouter",
                                         "model": "deepseek/deepseek-v4.1-flash"}}}

    def get_session_info(self, sid):
        return self.meta.get(sid, {})

    def update_session_metadata(self, sid, upd):
        self.meta.setdefault(sid, {}).update(upd)


def _task_agent(agent=None, bound="s-1"):
    registry = SimpleNamespace(resolve=lambda key: {"session_id": bound} if bound else None)
    container = SimpleNamespace(get_service=lambda name: registry)
    orch = SimpleNamespace(agents={"a": agent}) if agent else None
    return SimpleNamespace(container=container, session_manager=_FakeSessions(),
                           get_orchestrator=lambda sid: orch)


def _cmd(ta, words, chat_key="tg:dm:1"):
    from agents.task.model_switch_service import model_command
    return asyncio.run(model_command(ta, user_id="owner1", words=words, chat_key=chat_key))


@pytest.fixture
def no_env_write(monkeypatch):
    from core import config_service
    monkeypatch.setattr(config_service, "set_value",
                        lambda *a, **k: pytest.fail("/model must never write the env"))


def test_switch_queues_on_the_live_agent_and_pins_the_record(home, catalog, keyed, no_env_write):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    out = _cmd(ta, ["haiku", "reasoning=off"])
    assert out.startswith("✅") and "from my next step" in out and "until /new" in out
    assert agent.pinned["model"] == "anthropic/claude-haiku-5.5"
    assert agent.pinned["reasoning"] == "off"
    meta = ta.session_manager.meta["s-1"]
    assert meta["request"]["model"] == "anthropic/claude-haiku-5.5"
    assert meta["operator_pin"] is None and meta["model_pin"]["reasoning"] == "off"


def test_the_pin_carries_the_switch_so_a_reload_still_says_it(home, catalog, keyed, no_env_write):
    """095 W1a: switched_from + its time ride the pin; a recreated session re-learns
    them (remember_session_pin) and its identity line re-renders the owner's switch."""
    from agents.task.model_pin_state import forget_session, remember_session_pin, session_switch
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["haiku"])
    pin = ta.session_manager.meta["s-1"]["model_pin"]
    assert pin["switched_from"] == "deepseek/deepseek-v4.1-flash" and pin["switched_at"]
    forget_session("s-1")
    remember_session_pin("s-1", ta.session_manager.meta["s-1"])
    switched_from, at = session_switch("s-1", "anthropic/claude-haiku-5.5")
    assert switched_from == "deepseek/deepseek-v4.1-flash" and at == float(pin["switched_at"])
    assert session_switch("s-1", "some/fallback-model") is None   # a fallback is not the switch
    forget_session("s-1")


def test_failed_build_restores_the_record(home, catalog, keyed):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["haiku"])
    asyncio.run(agent.pinned["on_applied"]({"ok": False, "error": "boom"}))
    assert ta.session_manager.meta["s-1"]["request"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_a_failed_second_queued_switch_restores_the_record_before_the_first(home, catalog, keyed):
    """Two switches queued before a step: the first never ran, so a failure of the
    second puts back the record from BEFORE the first, never the first's pin."""
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["haiku"])
    _cmd(ta, ["haiku", "reasoning=off"])
    asyncio.run(agent.pinned["on_applied"]({"ok": False, "error": "boom"}))
    meta = ta.session_manager.meta["s-1"]
    assert meta["request"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert not meta.get("model_pin")


def test_reasoning_during_a_queued_switch_rides_the_queued_pin(home, catalog, keyed):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["haiku"])
    out = _cmd(ta, ["reasoning=high"])
    assert out.startswith("✅"), out
    assert agent.pinned["model"] == "anthropic/claude-haiku-5.5"
    assert agent.pinned["reasoning"] == "high"
    meta = ta.session_manager.meta["s-1"]
    assert meta["request"]["model"] == "anthropic/claude-haiku-5.5"
    assert meta["model_pin"]["reasoning"] == "high"


def test_reasoning_during_a_provider_fallback_keeps_the_sessions_own_model(home, catalog, keyed):
    """A fallback is temporary: /model reasoning= must not make it the session's pin."""
    agent = _FakeAgent()
    agent._first_model = SimpleNamespace(provider="openrouter", model="deepseek/deepseek-v4.1-flash")
    agent.model_name, agent.llm_provider = "z-ai/glm-5.2", "openrouter"
    ta = _task_agent(agent)
    out = _cmd(ta, ["reasoning=high"])
    assert out.startswith("✅"), out
    meta = ta.session_manager.meta["s-1"]
    assert meta["request"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert meta["model_pin"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_no_session_queues_for_the_next_chat(home, catalog, keyed):
    from agents.task.model_switch_service import owner_chat_pin
    ta = _task_agent(None, bound=None)
    out = _cmd(ta, ["haiku"], chat_key="tg:dm:9")
    assert "when this chat starts" in out
    assert owner_chat_pin("tg:dm:9", "owner1")["model"] == "anthropic/claude-haiku-5.5"
    assert owner_chat_pin("tg:dm:9", "owner1") is None   # consumed once


def test_a_call_with_no_chat_moves_only_the_default(home, catalog, keyed, no_env_write):
    """The console picker names no chat: the reply must not promise one."""
    from core.model_pins import chat_runtime_for
    ta = _task_agent(None, bound=None)
    out = _cmd(ta, ["glm", "default"], chat_key=None)
    assert out.startswith("✅") and "default for every new chat" in out
    assert "this chat" not in out and "next message" not in out
    assert chat_runtime_for("owner1")[1] == "z-ai/glm-5.3"
    out = _cmd(ta, ["haiku"], chat_key=None)
    assert not out.startswith("✅") and "default" in out
    assert chat_runtime_for("owner1")[1] == "z-ai/glm-5.3"


def test_default_writes_the_owner_store_not_the_env(home, catalog, keyed, no_env_write):
    from core.model_pins import chat_runtime_for
    ta = _task_agent(_FakeAgent())
    assert "also your default" in _cmd(ta, ["glm", "default"])
    assert chat_runtime_for("owner1")[1] == "z-ai/glm-5.3"
    out = _cmd(ta, ["reset", "default"])
    assert "Back to deepseek/deepseek-v4.1-flash" in out and "default cleared" in out


def test_show_names_the_live_model_and_the_tiers(home, catalog, keyed):
    out = _cmd(_task_agent(_FakeAgent()), [])
    assert "Model: deepseek/deepseek-v4.1-flash (openrouter)" in out
    assert "New chats start on:" in out
    # 121: the owner's settings and the switch for the agent's own choice
    assert "Heavy model: not set (it uses the main model)" in out
    assert "What I may choose for a helper, a goal or a job: free" in out
    assert "/model heavy <name>" in out and "/model choice free|listed" in out


def test_a_pricier_model_switches_at_once(home, catalog, keyed):
    out = _cmd(_task_agent(_FakeAgent()), ["sonnet"])
    assert "go" not in out.split() and "anthropic/claude-sonnet-5.5" in out


def test_alias_verbs(home):
    ta = _task_agent(_FakeAgent())
    assert "fav →" in _cmd(ta, ["alias", "fav", "openrouter/z-ai/glm-5.3"])
    assert "fav — openrouter/z-ai/glm-5.3" in _cmd(ta, ["aliases"])
    assert "removed" in _cmd(ta, ["alias", "fav", "-"])


# --- the swap itself ------------------------------------------------------------------- #

class _SwapAgent:
    pass


def _swap_agent():
    from agents.task.agent.core.model_swap import ModelSwapMixin

    class A(ModelSwapMixin):
        def __init__(self):
            self.logger = logging.getLogger("t")
            self.session_id = "s-swap"
            self.llm = SimpleNamespace(_default_temperature=0.3)
            self.calls = []
            mm = SimpleNamespace(model_name="m1", provider_name="openrouter", llm=self.llm)
            mm.set_runtime_identity = lambda m, p, switched_from=None, **kw: self.calls.append(
                ("identity", m, switched_from, kw.get("cause")))
            mm.rebuild_system_prompt = lambda native=None: self.calls.append(("prompt", native))
            mm.reset_usage_anchor = lambda reason="": self.calls.append(("anchor", reason))
            mm.recalibrate_for_model = lambda m: None
            self.message_manager = mm
            self.llm_provider = "openrouter"
            self.use_native_tools = True

        model_name = property(lambda s: s.message_manager.model_name,
                              lambda s, v: setattr(s.message_manager, "model_name", v))
        provider_name = property(lambda s: s.message_manager.provider_name,
                                 lambda s, v: setattr(s.message_manager, "provider_name", v))

        async def _create_llm_from_config_async(self, cfg, isolated=False):
            self.calls.append(("build", cfg, isolated))
            return SimpleNamespace(cfg=cfg)

        def _reconcile_native_tools(self, provider):
            return True

        def _provision_compaction_llm(self):
            self.calls.append(("aux", "compaction"))
            return "aux"
    return A()


def test_swap_rebuilds_what_the_model_owns():
    from agents.task.model_pin_state import session_reasoning
    a = _swap_agent()
    r = asyncio.run(a.swap_model("openrouter", "m2", reasoning="high"))
    assert r["ok"]
    build = next(c for c in a.calls if c[0] == "build")
    assert build[1]["temperature"] == 0.3 and build[2] is True   # kept + isolated
    assert ("identity", "m2", "m1", "owner") in a.calls
    assert ("prompt", True) in a.calls and ("anchor", "model change (owner)") in a.calls
    assert ("aux", "compaction") in a.calls and a.message_manager.aux_llm == "aux"
    assert session_reasoning("s-swap") == "high"


def test_pending_pin_applies_once_and_calls_back():
    a = _swap_agent()
    seen = []

    async def cb(res):
        seen.append(res["ok"])

    a.request_model_pin({"provider": "openrouter", "model": "m3", "on_applied": cb})
    assert asyncio.run(a.apply_pending_model_pin())["model"] == "m3"
    assert seen == [True] and asyncio.run(a.apply_pending_model_pin()) is None


def test_same_model_pin_changes_only_reasoning():
    from agents.task.model_pin_state import session_reasoning
    a = _swap_agent()
    a.request_model_pin({"provider": "openrouter", "model": "m1", "reasoning": "off"})
    res = asyncio.run(a.apply_pending_model_pin())
    assert res["unchanged"] and not any(c[0] == "build" for c in a.calls)
    assert session_reasoning("s-swap") == "off"


def test_the_drain_applies_a_queued_pin():
    from agents.task.agent.core.user_ingress import UserIngressMixin
    order = []

    class D(UserIngressMixin):
        logger = logging.getLogger("t")
        _pending_model_pin = {"model": "x"}

        async def apply_pending_model_pin(self):
            order.append("swap")

    d = D()

    async def drain():
        order.append("drain")
        return []

    d.hitl_manager = SimpleNamespace(drain_user_messages=drain)
    d.workspace_context = SimpleNamespace()
    try:
        asyncio.run(d._drain_user_messages())
    except Exception:
        pass
    assert order[:2] == ["swap", "drain"]


def test_message_manager_rebuild_keeps_a_profile_prompt():
    from agents.task.agent.message_manager.service import MessageManager
    mm = MessageManager.__new__(MessageManager)
    mm._custom_system_message = True
    assert mm.rebuild_system_prompt() is False


# --- the price rule for a sub-agent ---------------------------------------------------------- #

def test_child_model_rule(home, catalog, keyed):
    from agents.task.model_choice import child_model_check
    from core import model_pins
    parent = SimpleNamespace(model_name="deepseek/deepseek-v4.1-flash", user_id="owner1",
                             llm_provider="openrouter")
    run = lambda m: asyncio.run(child_model_check(parent, m, None))  # noqa: E731
    assert run("deepseek/deepseek-v4.1-flash") == ""
    assert run("anthropic/claude-sonnet-5.5") == ""        # 121: no price gate
    model_pins.set_choice("owner1", "listed")
    assert run("anthropic/claude-sonnet-5.5").startswith("refused")
    model_pins.set_alias("owner1", "strong", "openrouter/anthropic/claude-sonnet-5.5")
    assert run("anthropic/claude-sonnet-5.5") == ""
    model_pins.set_choice("owner1", "free")
    parent.model_name = "anthropic/claude-sonnet-5.5"
    assert run("anthropic/claude-haiku-5.5") == ""


def test_child_allowed_model_on_a_foreign_provider_is_refused(home, catalog, keyed, monkeypatch):
    """112 F3: an owner-chosen id is allowed on the owner's providers only."""
    from agents.task.model_choice import child_model_check
    monkeypatch.setenv("HEAVY_MODEL", "deepseek/deepseek-v4-pro")
    parent = SimpleNamespace(model_name="deepseek/deepseek-v4.1-flash", user_id="owner1",
                             llm_provider="openrouter")
    run = lambda m, p: asyncio.run(child_model_check(parent, m, p))  # noqa: E731
    assert run("deepseek/deepseek-v4-pro", None) == ""          # heavy, 5.2x input: allowed
    assert run("deepseek/deepseek-v4-pro", "openrouter") == ""
    assert "no key for provider 'together'" in run("deepseek/deepseek-v4-pro", "together")


# --- the owner-only job model ----------------------------------------------------------- #

def test_job_model_pin(home, catalog, keyed):
    from agents.task.model_switch_service import resolve_job_model
    pin, note = resolve_job_model("sonnet", "owner1")
    assert pin == {"model": "anthropic/claude-sonnet-5.5", "provider": "openrouter"} and note == ""
    pin, note = resolve_job_model("no/such-model", "owner1")
    assert pin is None and "not on openrouter" in note


def test_agent_job_tools_carry_a_model_and_no_provider():
    """121: the agent names the model of a goal or a job; the resolver finds the provider."""
    from tools.cronjob_tools import CronEditAction, CronScheduleAction
    from tools.goal_tools import GoalCreateAction, GoalUpdateAction
    for model in (CronScheduleAction, CronEditAction, GoalCreateAction, GoalUpdateAction):
        assert "model" in model.model_fields and "provider" not in model.model_fields


def test_reset_onto_the_operator_pin_follows_it_again(home, catalog, keyed):
    ta = _task_agent(_FakeAgent())
    _cmd(ta, ["haiku"])
    assert ta.session_manager.meta["s-1"]["operator_pin"] is None
    _cmd(ta, ["reset"])
    meta = ta.session_manager.meta["s-1"]
    assert meta["operator_pin"] == {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}
    assert meta["model_pin"] is None


def test_real_message_manager_rebuilds_the_prompt_for_the_new_model():
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.prompts import SystemPrompt
    llm = SimpleNamespace(model_name="gpt-5")
    mm = MessageManager(llm=llm, task="t", action_descriptions="acts",
                        system_prompt_class=SystemPrompt, max_input_tokens=100000)
    before = mm._system_message.content
    mm.model_name = "x-ai/grok-4.7"
    mm.provider_name = "openrouter"
    assert mm.rebuild_system_prompt(False) is True
    after = mm._system_message.content
    assert mm.use_native_tools is False and after != before
    assert mm._system_message_tokens > 0 and mm.system_prompt is mm._system_message
    mm.set_runtime_identity("x-ai/grok-4.7", "openrouter", switched_from="gpt-5")
    assert 'switched you to it from "gpt-5"' in mm._runtime_identity_message.content


def test_no_reasoning_level_for_a_model_without_one(home, catalog, keyed):
    from agents.task.model_pin_state import set_session_reasoning
    ta = _task_agent(_FakeAgent())
    out = _cmd(ta, ["llama", "reasoning=high"])
    assert out.startswith("❌") and "no reasoning setting" in out
    set_session_reasoning("s-1", "high")          # a level kept from a reasoning model…
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["llama"])
    assert agent.pinned["reasoning"] is None      # …is dropped for one without it


# --- revalidation fixes (2026-10-30 review) -------------------------------------------- #

def test_a_stale_binding_queues_for_the_next_chat(home, catalog, keyed, monkeypatch):
    import core.surfaces.session_policy as sp
    from agents.task.model_switch_service import owner_chat_pin
    monkeypatch.setattr(sp, "should_start_fresh", lambda row, **kw: (True, "idle"))
    monkeypatch.setattr(sp, "with_agent_activity", lambda row, c: row)
    agent = _FakeAgent()
    ta = _task_agent(agent)
    out = _cmd(ta, ["haiku"], chat_key="tg:dm:stale")
    assert "when this chat starts" in out and agent.pinned is None
    assert owner_chat_pin("tg:dm:stale", "owner1")["model"] == "anthropic/claude-haiku-5.5"


def test_failed_switch_restores_the_record_exactly(home, catalog, keyed):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    ta.session_manager.meta["s-1"]["operator_pin"] = {"provider": "openrouter",
                                                      "model": "deepseek/deepseek-v4.1-flash"}
    _cmd(ta, ["haiku", "reasoning=low"])
    asyncio.run(agent.pinned["on_applied"]({"ok": False, "error": "boom"}))
    meta = ta.session_manager.meta["s-1"]
    assert meta["operator_pin"] == {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}
    assert meta["request"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_failed_switch_on_an_unpinned_record_leaves_no_pin(home, catalog, keyed):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    assert "model_pin" not in ta.session_manager.meta["s-1"]
    _cmd(ta, ["haiku", "reasoning=low"])
    asyncio.run(agent.pinned["on_applied"]({"ok": False, "error": "boom"}))
    meta = ta.session_manager.meta["s-1"]
    assert not meta.get("model_pin") and not meta.get("model")
    assert meta["request"]["model"] == "deepseek/deepseek-v4.1-flash"


def test_reset_drops_a_switch_queued_before_it(home, catalog, keyed):
    agent = _FakeAgent()
    ta = _task_agent(agent)
    _cmd(ta, ["haiku"])
    assert agent.pinned["model"] == "anthropic/claude-haiku-5.5"
    out = _cmd(ta, ["reset"])
    assert "Back to deepseek/deepseek-v4.1-flash" in out
    assert agent.pinned is None


def test_unreadable_aliases_are_named_not_empty(home):
    from core import model_pins
    model_pins.set_alias("owner1", "fav", "openrouter/x/y")
    next(home.rglob("model.json")).write_text("{bad")
    table, err = model_pins.aliases_read("owner1")
    assert table == {} and err
    text = "\n".join(ms.alias_lines(table, read_error=err))
    assert "could not be read" in text
    out = _cmd(_task_agent(_FakeAgent()), ["aliases"])
    assert "could not be read" in out


def test_variable_or_missing_prices_fail_closed(catalog, monkeypatch):
    from modules.llm import openrouter_catalog as oc
    rows = oc._parse({"data": [
        {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"},
         "supported_parameters": ["tools"]},
        {"id": "x/noprice", "pricing": {}, "supported_parameters": ["tools"]}]})
    assert rows["openrouter/auto"].price is None and rows["x/noprice"].price is None


def test_child_on_a_model_with_no_price_runs_with_a_warning(home, catalog, keyed, monkeypatch):
    from agents.task.model_choice import child_model_check
    from modules.llm import openrouter_catalog as oc
    data = {"data": CATALOG["data"] + [{"id": "openrouter/auto", "context_length": 1,
                                        "pricing": {"prompt": "-1", "completion": "-1"},
                                        "supported_parameters": ["tools"]}]}
    monkeypatch.setattr(oc, "_fetch", lambda: data)
    parent = SimpleNamespace(model_name="anthropic/claude-sonnet-5.5", user_id="owner1",
                             llm_provider="openrouter")
    route = SimpleNamespace(notes=[])
    assert asyncio.run(child_model_check(parent, "openrouter/auto", None, route)) == ""
    assert route.notes == ["openrouter/auto: price unknown (warning: its price is unknown, so "
                           "only the run spend cap bounds its cost)"]


def test_failed_list_read_is_not_retried_at_once(catalog, monkeypatch):
    from modules.llm import openrouter_catalog as oc
    catalog["fail"] = True
    assert oc.catalog() is None
    calls = catalog["calls"]
    assert oc.catalog() is None and catalog["calls"] == calls   # cooled down


def test_job_pin_always_names_its_provider(home, catalog, keyed):
    from agents.task.model_switch_service import resolve_job_model
    assert resolve_job_model("sonnet", "owner1")[0] == {
        "model": "anthropic/claude-sonnet-5.5", "provider": "openrouter"}


def test_unreadable_default_is_named(home):
    from core import model_pins
    model_pins.set_default("owner1", "openrouter", "z-ai/glm-5.3")
    next(home.rglob("model.json")).write_text("{bad")
    assert "could not be read" in model_pins.chat_runtime_for("owner1")[3]


def test_api_request_with_no_model_takes_the_owner_default(home):
    from agents.task.model_switch_service import with_chat_pin
    from core import model_pins
    assert with_chat_pin({"task": "t"}, "owner1") == {"task": "t"}
    model_pins.set_default("owner1", "openrouter", "z-ai/glm-5.3", "off")
    body = with_chat_pin({"task": "t"}, "owner1")
    assert body["model"] == "z-ai/glm-5.3" and body["_chat_pin"]["reasoning"] == "off"
    assert with_chat_pin({"task": "t", "model": "x"}, "owner1") == {"task": "t", "model": "x"}


def test_rebuilt_prompt_has_no_skills_placeholder():
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.prompts import SystemPrompt
    mm = MessageManager(llm=SimpleNamespace(model_name="gpt-5"), task="t", action_descriptions="a",
                        system_prompt_class=SystemPrompt, max_input_tokens=100000)
    assert mm.rebuild_system_prompt() is True
    assert "{SKILLS_PLACEHOLDER}" not in mm._system_message.content


def test_a_second_swap_closes_the_client_the_first_built():
    a = _swap_agent()
    closed = []

    class _C:
        async def cleanup(self):
            closed.append(1)

    async def build(cfg, isolated=False):
        return SimpleNamespace(cfg=cfg, _client=_C())

    a._create_llm_from_config_async = build
    asyncio.run(a.swap_model("openrouter", "m2"))
    assert closed == []                      # the shared client is never closed
    asyncio.run(a.swap_model("openrouter", "m3"))
    assert closed == [1]                     # the one the first swap built is


# --- 121: the agent names any model; the price rule bounds it ----------------------------- #

def _parent(model="deepseek/deepseek-v4.1-flash", **kw):
    return SimpleNamespace(model_name=model, user_id="owner1", llm_provider="openrouter", **kw)


def _check(parent, model, provider=None, route=None):
    from agents.task.model_choice import child_model_check
    return asyncio.run(child_model_check(parent, model, provider, route))


def test_a_child_runs_on_any_model(home, catalog, keyed):
    """Not the owner's, not cheaper — and it runs; the result names the price."""
    route = SimpleNamespace(notes=[])
    assert _check(_parent(), "z-ai/glm-5.3", route=route) == ""      # 2.8x the output price
    assert route.notes == ["z-ai/glm-5.3: $0.07 in / $1.4 out per M"]
    assert _check(_parent(), "anthropic/claude-haiku-5.5") == ""


def test_a_pricier_model_runs_attended_or_not(home, catalog, keyed, monkeypatch):
    route = SimpleNamespace(notes=[])
    assert _check(_parent(), "anthropic/claude-sonnet-5.5", route=route) == ""
    assert route.notes == ["anthropic/claude-sonnet-5.5: $2 in / $10 out per M"]
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    assert _check(_parent(), "anthropic/claude-sonnet-5.5") == ""


def test_what_the_price_rule_refuses(home, catalog, keyed):
    assert "is not a model of openrouter" in _check(_parent(), "no/such-model")
    assert "cannot call tools" in _check(_parent("anthropic/claude-sonnet-5.5"), "openai/gpt-image-x")
    assert "no key for provider 'together'" in _check(_parent(), "z-ai/glm-5.3", "together")


def test_listed_only(home, catalog, keyed):
    from core import model_pins
    model_pins.set_choice("owner1", "listed")
    assert "only the models the owner uses or listed" in _check(_parent(), "z-ai/glm-5.3")
    assert _check(_parent(), "deepseek/deepseek-v4.1-flash") == ""
    model_pins.set_alias("owner1", "g", "openrouter/z-ai/glm-5.3")
    assert _check(_parent(), "z-ai/glm-5.3") == ""


def test_the_models_read(home, catalog, keyed, monkeypatch):
    from agents.task.model_choice import models_view
    from core import model_pins
    monkeypatch.setenv("HEAVY_MODEL", "anthropic/claude-sonnet-5.5")
    text = asyncio.run(models_view("owner1", ("openrouter", "deepseek/deepseek-v4.1-flash")))
    assert "You run on deepseek/deepseek-v4.1-flash (openrouter): $0.045 in / $0.5 out" in text
    assert "- deepseek/deepseek-v4.1-flash (main)" in text
    assert "- anthropic/claude-sonnet-5.5 (heavy): $2 in / $10 out" in text
    assert "- openrouter/z-ai/glm-5.3: $0.07 in / $1.4 out per M, context 1,048,576" in text
    assert "gpt-image-x" not in text                                  # it cannot call tools
    assert text.index("claude-haiku-5.5") < text.index("z-ai/glm-5.3")   # cheapest first
    catalog["fail"] = True
    from modules.llm import openrouter_catalog as oc
    monkeypatch.setattr(oc, "_MEM", {"at": 0.0, "rows": None})
    monkeypatch.setattr(oc, "_cache_path", lambda: home / "none.json")
    assert "Could not read the model list of: openrouter" in asyncio.run(
        models_view("owner1", ("openrouter", "deepseek/deepseek-v4.1-flash")))
    model_pins.set_choice("owner1", "listed")
    text = asyncio.run(models_view("owner1", ("openrouter", "deepseek/deepseek-v4.1-flash")))
    assert "ONLY the listed models" in text and "Other models" not in text


def test_a_raw_model_choice_is_recorded_with_its_reason(monkeypatch):
    from tools.controller import delegate_tier as dt
    from tools.controller.views import SubtaskAction
    seen = []
    monkeypatch.setattr("core.model_routing.record_model_route", lambda **kw: seen.append(kw))
    dt.resolve_delegate_tiers(parent_agent=_parent(), model="z-ai/glm-5.3", provider=None,
                              tier=None, why="a second opinion", tasks=None, user_id="owner1")
    assert [(r["tier"], r["model"], r["why"]) for r in seen] == [
        ("model", "z-ai/glm-5.3", "a second opinion")]
    seen.clear()
    tasks = [SubtaskAction(task="a focused task of more than twenty chars", model="x/y", why="w"),
             SubtaskAction(task="a focused task of more than twenty chars")]
    dt.resolve_delegate_tiers(parent_agent=_parent(), model=None, provider=None, tier=None,
                              why=None, tasks=tasks, user_id="owner1")
    assert [(r["model"], r["why"]) for r in seen] == [("x/y", "w")]


# --- 121 phase 2: the agent names the model of a goal or a job ---------------------------- #

def _job_model(model, *, owner_turn=False, existing=None, tier=None, why="needs it"):
    """``(updates, refusal, note)``; the owner notice is appended to ``_job_model.told``."""
    from tools.model_tier import model_payload
    updates, refusal, note, notice = asyncio.run(model_payload(
        model, why, tier=tier, owner_turn=owner_turn, user_id="owner1", existing=existing,
        what="job `j1`"))
    if notice:
        _job_model.told.append(notice)
    return updates, refusal, note


_job_model.told = []


def test_an_unattended_run_sets_a_job_model(home, catalog, keyed):
    updates, refusal, note = _job_model("z-ai/glm-5.3")
    assert refusal is None
    assert updates == {"model": "z-ai/glm-5.3", "provider": "openrouter", "model_by": "agent",
                       "model_why": "needs it", "tier": None, "tier_why": None}
    assert "z-ai/glm-5.3 (openrouter), $0.07 in / $1.4 out" in note and "set by the agent" in note
    assert _job_model(None) == ({}, None, "") and _job_model("  ") == ({}, None, "")
    assert "not both" in _job_model("z-ai/glm-5.3", tier="heavy")[1]
    assert _job_model("haiku")[0]["model"] == "anthropic/claude-haiku-5.5"      # an alias resolves


def test_a_pricier_job_model_is_set_unattended(home, catalog, keyed):
    updates, refusal, note = _job_model("anthropic/claude-sonnet-5.5")
    assert refusal is None and updates["model"] == "anthropic/claude-sonnet-5.5"
    assert "$2 in / $10 out" in note
    assert "is not on openrouter" in _job_model("no/such-model")[1]
    updates, refusal, _ = _job_model("sonnet", owner_turn=True)
    assert refusal is None and updates["model_by"] == "owner"


def test_the_owners_model_is_locked_and_an_unattended_change_is_told(home, catalog, keyed):
    told = _job_model.told = []
    locked = {"model": "anthropic/claude-haiku-5.5", "model_by": "owner"}
    old_pin = {"model": "anthropic/claude-haiku-5.5"}      # the owner CLI before 121
    assert "only the owner changes it" in _job_model("z-ai/glm-5.3", existing=old_pin)[1]
    assert "only the owner changes it" in _job_model("z-ai/glm-5.3", existing=locked)[1]
    assert "only the owner changes it" in _job_model("default", existing=locked)[1]
    assert _job_model("z-ai/glm-5.3", existing=locked, owner_turn=True)[1] is None
    assert told == []                                   # the owner's own turn needs no notice
    mine = {"model": "anthropic/claude-haiku-5.5", "model_by": "agent", "provider": "openrouter"}
    updates, refusal, _ = _job_model("z-ai/glm-5.3", existing=mine)
    assert refusal is None and updates["model"] == "z-ai/glm-5.3"
    assert len(told) == 1 and "job `j1`" in told[0] and "z-ai/glm-5.3" in told[0]
    _job_model("z-ai/glm-5.3", existing={})             # a create tells nothing
    assert len(told) == 1
    updates, refusal, note = _job_model("default", existing=mine)
    assert updates == {"model": None, "model_by": None, "model_why": None, "provider": None}
    assert "back to the default" in note
    assert len(told) == 2 and "back to the default" in told[1]   # an unattended clear is told


def test_every_run_records_its_model(monkeypatch):
    from core import model_routing
    seen = []
    monkeypatch.setattr(model_routing, "record_model_route", lambda **kw: seen.append(kw))
    model_routing.record_stored_tier(kind="job", payload={}, model="x/main", absent=False)
    model_routing.record_stored_tier(kind="goal", model="x/pick", absent=False, payload={
        "model": "x/pick", "model_by": "agent", "model_why": "long report"})
    assert [(r["kind"], r["tier"], r["model"], r["why"]) for r in seen] == [
        ("job", "default", "x/main", ""),
        ("goal", "model", "x/pick", "set by the agent: long report")]


def test_the_owner_cli_pin_is_locked(home, catalog, keyed):
    from cli.commands.cron import _job_model_updates
    assert _job_model_updates("haiku", "owner1")["model_by"] == "owner"
    assert _job_model_updates("none", "owner1") == {
        "model": None, "provider": None, "model_by": None, "model_why": None}


# --- 121 phase 3: the owner's settings on a chat seat --------------------------------------- #

@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner1")


@pytest.mark.parametrize("words,action,tier,name", [
    (["choice"], "choice", "", ""),
    (["choice", "listed"], "choice", "", "listed"),
    (["heavy", "sonnet"], "set", "heavy", "sonnet"),
    (["light", "openrouter", "x/y"], "set", "light", "x/y"),
    (["heavy", "reset"], "reset", "heavy", ""),
    (["heavy"], "set", "", "heavy"),            # alone: this chat moves to the heavy model
])
def test_parse_the_owner_settings(words, action, tier, name):
    cmd = ms.parse_model_args(words)
    assert (cmd.action, cmd.tier, cmd.name) == (action, tier, name), cmd.error


@pytest.mark.parametrize("words", [["choice", "any"], ["choice", "free", "now"],
                                   ["heavy", "sonnet", "default"], ["light", "sonnet", "reasoning=low"]])
def test_parse_refuses_a_bad_setting(words):
    assert ms.parse_model_args(words).action == "error"


def test_the_owner_sets_the_heavy_model_from_chat(home, catalog, keyed, owner):
    from core.runtime_config import heavy_model_pin, tier_models
    ta = _task_agent(_FakeAgent())
    assert heavy_model_pin() is None
    out = _cmd(ta, ["heavy", "sonnet"])                    # 44x the main model: no gate
    assert out.startswith("✅ heavy: anthropic/claude-sonnet-5.5 (openrouter)") and "next run" in out
    assert heavy_model_pin() == "anthropic/claude-sonnet-5.5"
    assert tier_models("openrouter")["heavy"] == "anthropic/claude-sonnet-5.5"
    assert ta.session_manager.meta == {} or "model_pin" not in ta.session_manager.meta.get("s-1", {})
    assert "Heavy model: anthropic/claude-sonnet-5.5 — set by you" in _cmd(ta, [])
    # the owner's setting outranks the env; a what-if env is not touched
    assert heavy_model_pin({"HEAVY_MODEL": "x/env"}) == "x/env"
    assert _cmd(ta, ["heavy", "reset"]).startswith("✅ heavy: back to the operator's value")
    assert heavy_model_pin() is None


def test_the_light_setting_and_what_it_refuses(home, catalog, keyed, owner, monkeypatch):
    from core.runtime_config import tier_models
    ta = _task_agent(_FakeAgent())
    assert _cmd(ta, ["light", "haiku"]).startswith("✅ light: anthropic/claude-haiku-5.5")
    assert tier_models("openrouter")["light"] == "anthropic/claude-haiku-5.5"
    assert _cmd(ta, ["light", "no-such-model-zz"]).startswith("❌")
    # another tenant sets nothing
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "someone-else")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert "Only the owner" in _cmd(ta, ["heavy", "haiku"])


def test_the_choice_switch_from_chat(home, catalog, keyed, owner):
    from core import model_choice as mc
    ta = _task_agent(_FakeAgent())
    assert _cmd(ta, ["choice"]).startswith("Model choice: free")
    assert _cmd(ta, ["choice", "listed"]).startswith("✅ Model choice: listed")
    assert mc.choice_mode("owner1") == "listed"
    assert "What I may choose for a helper, a goal or a job: listed" in _cmd(ta, [])
    assert _cmd(ta, ["choice", "free"]).startswith("✅ Model choice: free")


def test_the_last_choices_of_the_agent(tmp_path, monkeypatch):
    from agents.task import model_settings
    from core import event_log
    log = event_log.TelemetryEventLog(str(tmp_path / "telemetry.db"))
    monkeypatch.setattr(event_log, "get_event_log", lambda *a, **k: log)
    assert model_settings.recent_choices("owner1") == []          # nothing chosen yet
    for attrs in ({"kind": "job", "tier": "default", "model": "x/main"},
                  {"kind": "child", "tier": "model", "model": "x/mid", "why": "a second opinion"},
                  {"kind": "goal", "tier": "heavy", "model": "x/strong", "why": "decides a payout"}):
        log.record("model_routed", user_id="owner1", source="test", attrs=attrs)
    assert model_settings.recent_choices("owner1") == [
        "My last model choices (newest first):",
        "  goal: x/strong (heavy) — decides a payout",
        "  helper: x/mid — a second opinion"]

    def _unreadable(*a, **k):
        raise RuntimeError("the event log is not readable")
    monkeypatch.setattr(log, "query", _unreadable)
    assert model_settings.recent_choices("owner1") == [
        "My last model choices could not be read (RuntimeError)."]


def test_the_owner_settings_from_a_terminal(home, catalog, keyed, owner, monkeypatch):
    from click.testing import CliRunner
    from cli.commands.model import model
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid=None: "owner1")
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda *a, **k: None)
    run = CliRunner().invoke
    assert "Model choice: listed" in run(model, ["choice", "listed"]).output
    assert "light: anthropic/claude-haiku-5.5" in run(model, ["light", "haiku"]).output
    out = run(model, ["heavy"]).output
    assert "Light model: anthropic/claude-haiku-5.5 — set by you" in out and "listed" in out


def test_a_job_model_with_no_price_is_set_with_a_warning(home, catalog, keyed, monkeypatch):
    from modules.llm import openrouter_catalog as oc
    data = {"data": CATALOG["data"] + [{"id": "vendor/variable", "context_length": 1,
                                        "pricing": {"prompt": "-1", "completion": "-1"},
                                        "supported_parameters": ["tools"]}]}
    monkeypatch.setattr(oc, "_fetch", lambda: data)
    updates, refusal, note = _job_model("vendor/variable")          # an unattended run
    assert refusal is None and updates["model"] == "vendor/variable"
    assert "price unknown" in note and "Warning: its price is unknown" in note


def test_unreadable_model_settings_are_named(home, catalog, keyed, owner):
    from core import model_pins
    path = model_pins._path("owner1", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    out = _cmd(_task_agent(_FakeAgent()), [])
    assert "Your model settings could not be read" in out
    assert "What I may choose for a helper, a goal or a job: listed" in out



# --- 121 revalidation: the review of phases 1-3 -------------------------------------------- #

@pytest.mark.parametrize("words,action,tier,name,default", [
    (["heavy", "go"], "set", "", "heavy", False),          # an old confirm line
    (["heavy", "default"], "set", "", "heavy", True),      # heavy for new chats
    (["light"], "set", "", "light", False),
    (["light", "reasoning=low"], "set", "", "light", False),
    (["heavy", "sonnet", "go"], "set", "heavy", "sonnet", False),
])
def test_the_chat_forms_of_heavy_and_light_still_move_the_chat(words, action, tier, name,
                                                              default):
    cmd = ms.parse_model_args(words)
    assert (cmd.action, cmd.tier, cmd.name, cmd.default) == (action, tier, name, default), cmd.error


def test_light_is_a_symbolic_alias(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_MODEL", "deepseek/deepseek-v4.1-flash")
    r = ms.resolve_name("light", operator_provider="openrouter", keyed_providers=["openrouter"])
    assert r.model and r.via == "the light model"


def test_a_job_model_named_heavy_or_by_two_words(home, catalog, keyed, monkeypatch):
    monkeypatch.setenv("HEAVY_MODEL", "anthropic/claude-haiku-5.5")
    assert _job_model("heavy")[0]["model"] == "anthropic/claude-haiku-5.5"
    assert _job_model("openrouter z-ai/glm-5.3")[0]["model"] == "z-ai/glm-5.3"
    assert "is not a model name" in _job_model("a b c")[1]


def test_a_listed_id_on_another_provider_is_not_listed(home, catalog, keyed):
    from agents.task.model_choice import is_listed
    from core import model_pins
    model_pins.set_alias("owner1", "strong", "openrouter/anthropic/claude-sonnet-5.5")
    assert is_listed("owner1", "", "anthropic/claude-sonnet-5.5")
    assert is_listed("owner1", "openrouter", "anthropic/claude-sonnet-5.5")
    assert not is_listed("owner1", "anthropic", "anthropic/claude-sonnet-5.5")
    assert not is_listed("owner1", "openrouter", "x/other")


def test_a_stored_model_is_the_record_even_beside_a_tier(monkeypatch):
    from core import model_routing
    seen = []
    monkeypatch.setattr(model_routing, "record_model_route", lambda **kw: seen.append(kw))
    model_routing.record_stored_tier(kind="job", model="x/owner", absent=False, payload={
        "model": "x/owner", "model_by": "owner", "tier": "light", "tier_why": "old"})
    assert (seen[0]["tier"], seen[0]["why"]) == ("model", "set by the owner")


def test_the_owner_cli_model_clears_a_stored_tier(home, catalog, keyed):
    from cli.commands.cron import _job_model_updates
    out = _job_model_updates("haiku", "owner1")
    assert out["tier"] is None and out["tier_why"] is None


def test_the_settings_live_where_they_are_read(home, catalog, keyed, monkeypatch):
    """The local seat writes under the bound owner principal, which the routing reads."""
    from core import model_pins
    from core.runtime_config import tier_models
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner1")
    monkeypatch.setattr("core.instance.is_owner_uid", lambda uid, **k: True)
    ta = _task_agent(_FakeAgent())
    from agents.task.model_switch_service import model_command
    out = asyncio.run(model_command(ta, user_id="local", words=["light", "haiku"],
                                    chat_key="telegram:1", seat="repl"))
    assert out.startswith("✅ light:")
    assert model_pins.owner_tier("owner1", "light")["model"] == "anthropic/claude-haiku-5.5"
    assert model_pins.owner_tier("local", "light") is None
    assert tier_models("openrouter")["light"] == "anthropic/claude-haiku-5.5"
