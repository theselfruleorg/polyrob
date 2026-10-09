"""033: the Controller effect hooks — the recorder seam and the pause gate.

⚠️ Every pause here passes ``data_dir`` and patches ``state_bases``: a bare
``pause(...)`` pauses the developer's real data home.
"""
import types

import pytest

import core.event_log as el


class _Registry:
    def __init__(self, mapping):
        self._m = mapping

    def get_action(self, name):
        return self._m.get(name)


class _Controller:
    _is_sub_agent = False
    session_id = "s1"

    def __init__(self, mapping):
        self.registry = _Registry(mapping)

    def get_action_details(self, name):
        return self.registry.get_action(name)


class _Forged:
    user_id = "u1"
    session_id = "s1"
    role = "leaf"
    is_sub_agent = False
    metadata = {"turn_kind": "self_wake"}


class _Owner:
    user_id = "u1"
    session_id = "s1"
    role = "orchestrator"
    is_sub_agent = False
    metadata = {}


def _act(tool):
    return types.SimpleNamespace(tool=tool, function=None)


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log()
    el._INSTANCES.clear()


@pytest.fixture
def paused(tmp_path, monkeypatch):
    import core.autonomy_control as ac
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ac, "state_bases", lambda d=None: [str(home)])

    def _pause(*scopes):
        ac.pause(str(home), scopes=tuple(scopes), set_by="test", via="unit")
    return _pause


@pytest.fixture
def owner_turn(monkeypatch):
    monkeypatch.setattr(
        "tools.controller.turn_origin._is_forged_or_autonomous_turn",
        lambda ctx, c: False)


# --- the recorder ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_hook_records_a_write(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    c = _Controller({"twitter_reply": _act("twitter")})
    await make_effect_record_hook(c)(
        "twitter_reply", {"text": "hi"},
        types.SimpleNamespace(error=None, extracted_content="ok"), _Forged())
    rows = tlog().query(kind="external_write", effect="social")
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert a["tool"] == "twitter" and a["action"] == "twitter_reply"
    assert a["autonomous"] is True and a["outcome"] == "ok"
    assert a["surface"] == "self_wake"
    assert rows[0]["user_id"] == "u1"


@pytest.mark.asyncio
async def test_post_hook_records_failures_too(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    c = _Controller({"twitter_post": _act("twitter")})
    await make_effect_record_hook(c)(
        "twitter_post", {}, types.SimpleNamespace(error="429"), _Forged())
    assert tlog().query(kind="external_write")[0]["attrs"]["outcome"] == "error"


@pytest.mark.asyncio
async def test_read_actions_record_nothing(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    c = _Controller({"twitter_search": _act("twitter"),
                     "filesystem_write_file": _act("filesystem")})
    hook = make_effect_record_hook(c)
    await hook("twitter_search", {}, types.SimpleNamespace(error=None), _Forged())
    await hook("filesystem_write_file", {}, types.SimpleNamespace(error=None), _Forged())
    assert tlog().query(kind="external_write") == []


@pytest.mark.asyncio
async def test_mcp_read_only_hint_cannot_suppress_the_record(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook

    async def fn():
        return None
    fn._mcp_read_only = True
    c = _Controller({"srv_list": types.SimpleNamespace(tool="mcp", function=fn),
                     "srv_write": _act("mcp")})
    hook = make_effect_record_hook(c)
    await hook("srv_list", {}, None, _Forged())
    await hook("srv_write", {}, None, _Forged())
    rows = tlog().query(kind="external_write")
    assert {r["attrs"]["action"] for r in rows} == {"srv_list", "srv_write"}
    assert rows[0]["effect"] == "network"


@pytest.mark.asyncio
async def test_record_hook_never_raises():
    from tools.controller.effect_hooks import make_effect_record_hook

    class _Boom:
        def get_action_details(self, n):
            raise RuntimeError("boom")
    await make_effect_record_hook(_Boom())("x", {}, None, None)


# --- the gate --------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action,tool,scope", [
    ("twitter_post", "twitter", "social"),        # social -> social_post
    ("defi_trade_swap", "defi_trade", "all"),     # money -> spend
    ("email_send", "email", "all"),               # comms -> outbound_comms
    ("message", None, "all"),                     # comms, a tool-less closure
    ("git_push", "git", "oversight"),             # public -> oversight_deploy
    ("app_service_deploy", "app_service", "apps"),  # public -> app_deploy
])
async def test_autonomous_write_is_denied_under_its_pause(tlog, paused, action, tool, scope):
    from tools.controller.effect_hooks import make_effect_gate_hook
    paused(scope)
    c = _Controller({action: _act(tool)} if tool else {})
    reason = await make_effect_gate_hook(c)(action, {}, _Forged())
    assert reason and "paused" in reason
    row = tlog().query(kind="external_write")[0]
    assert row["attrs"]["outcome"] == "denied" and row["attrs"]["action"] == action


@pytest.mark.asyncio
async def test_a_pause_that_does_not_cover_the_effect_allows(paused):
    from tools.controller.effect_hooks import make_effect_gate_hook
    paused("social")
    c = _Controller({"defi_trade_swap": _act("defi_trade")})
    assert await make_effect_gate_hook(c)("defi_trade_swap", {}, _Forged()) is None


@pytest.mark.asyncio
async def test_spend_pause_does_not_refuse_the_x402_sweep_read(tlog, paused):
    """067 P0.6: x402_sweep never pays, so the spend pause must not refuse it."""
    from tools.controller.effect_hooks import make_effect_gate_hook
    paused("all")
    c = _Controller({"x402_pay_x402_sweep": _act("x402_pay"),
                     "x402_pay_x402_fetch": _act("x402_pay")})
    hook = make_effect_gate_hook(c)
    assert await hook("x402_pay_x402_sweep", {}, _Forged()) is None
    assert await hook("x402_pay_x402_fetch", {}, _Forged())  # the payer stays gated
    assert [r["attrs"]["action"] for r in tlog().query(kind="external_write")] == [
        "x402_pay_x402_fetch"]


_DEFI_SPEND_VERBS = (
    "defi_trade_bridge", "defi_trade_wrap", "defi_trade_unwrap", "defi_trade_call",
    "defi_trade_deploy_contract", "defi_trade_deploy_token",
    "defi_trade_solana_deploy_token", "defi_trade_solana_transfer", "defi_trade_lp_add", "defi_trade_lp_remove",
    "defi_trade_lp_collect", "defi_trade_nft_transfer",
    "defi_trade_nft_revoke_approval", "defi_trade_register_agent",
    "defi_trade_set_agent_uri",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", _DEFI_SPEND_VERBS)
async def test_strict_mode_classifies_each_defi_spend_verb(monkeypatch, owner_turn, action):
    """067 P0.7: each verb has an explicit money row, so strict mode does not
    refuse it as unclassified."""
    from core.effects import classify_effect
    from tools.controller.effect_hooks import make_effect_gate_hook
    v = classify_effect("defi_trade", action)
    assert (v.effect, v.confidence) == ("money", "action")
    monkeypatch.setenv("EXTERNAL_WRITE_STRICT", "true")
    c = _Controller({action: _act("defi_trade")})
    assert await make_effect_gate_hook(c)(action, {}, _Owner()) is None


@pytest.mark.asyncio
async def test_owner_turn_is_never_denied(paused, owner_turn):
    from tools.controller.effect_hooks import make_effect_gate_hook
    paused("all")
    c = _Controller({"twitter_post": _act("twitter"), "defi_trade_swap": _act("defi_trade")})
    hook = make_effect_gate_hook(c)
    assert await hook("twitter_post", {}, _Owner()) is None
    assert await hook("defi_trade_swap", {}, _Owner()) is None


@pytest.mark.asyncio
async def test_observe_only_classes_are_not_gated(paused):
    from tools.controller.effect_hooks import make_effect_gate_hook
    paused("all")
    c = _Controller({"srv_do": _act("mcp"), "shell_run": _act("shell"),
                     "skill_manage": None})
    hook = make_effect_gate_hook(c)
    assert await hook("srv_do", {}, _Forged()) is None          # network
    assert await hook("shell_run", {}, _Forged()) is None       # code
    assert await hook("skill_manage", {}, _Forged()) is None    # self


@pytest.mark.asyncio
async def test_gate_flag_off_allows(paused, monkeypatch):
    from tools.controller.effect_hooks import make_effect_gate_hook
    monkeypatch.setenv("EXTERNAL_WRITE_PAUSE_GATE", "off")
    paused("all")
    c = _Controller({"twitter_post": _act("twitter")})
    assert await make_effect_gate_hook(c)("twitter_post", {}, _Forged()) is None


@pytest.mark.asyncio
async def test_strict_refuses_an_unclassified_writer(monkeypatch):
    from tools.controller.effect_hooks import make_effect_gate_hook
    monkeypatch.setenv("EXTERNAL_WRITE_STRICT", "on")
    c = _Controller({"twitter_new_verb": _act("twitter"), "twitter_post": _act("twitter"),
                     "srv_do": _act("mcp")})
    hook = make_effect_gate_hook(c)
    assert "unclassified" in (await hook("twitter_new_verb", {}, _Owner()) or "")
    assert await hook("twitter_post", {}, _Owner()) is None     # explicit row
    assert await hook("srv_do", {}, _Owner()) is None           # MCP is exempt


@pytest.mark.asyncio
async def test_gate_fails_closed_through_the_pipeline(monkeypatch):
    """A crashing pause probe must DENY, not silently allow."""
    from tools.controller.effect_hooks import make_effect_gate_hook
    from tools.controller.hooks import HookPipeline

    def boom(*a, **k):
        raise RuntimeError("pause record unreadable")
    monkeypatch.setattr("core.effects.effect_pause_refusal", boom)
    pipe = HookPipeline()
    pipe.register_pre(make_effect_gate_hook(_Controller({"twitter_post": _act("twitter")})),
                      fail_mode="closed")
    reason = await pipe.run_pre("twitter_post", {}, _Forged())
    assert reason and "guardrail" in reason


def test_controller_registers_both_hooks():
    """Registered in Controller.__init__, so a delegated child's fresh Controller
    carries them too."""
    from tools.controller.service import Controller
    orch = types.SimpleNamespace(session_id="s", user_id="u", workspace_dir="/tmp/ws")
    c = Controller(orchestrator=orch)
    pre = [getattr(h[0] if isinstance(h, tuple) else h, "__name__", "")
           for h in c._hooks.pre]
    post = [getattr(h[0] if isinstance(h, tuple) else h, "__name__", "")
            for h in c._hooks.post]
    assert "_effect_gate_hook" in pre
    assert "_effect_record_hook" in post
