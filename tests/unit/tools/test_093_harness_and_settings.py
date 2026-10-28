"""093 WS-C + WS-D, the §6 flow in unit form.

Prod 2026-10-28: MCP_ENABLED on, zero servers. The agent told the owner four
wrong causes in two days. Pass = the agent reads the TRUE reason with who/when
from ``agent_status(tool="mcp")``, puts ONE ``/mcp add`` card to the owner, and
the owner's Confirm runs that line on the seat. An autonomous or correspondent
caller is refused, a money setting is refused, an operator setting is filed.
"""
import asyncio
from types import SimpleNamespace

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from core.surfaces import cards


class _Container:
    def __init__(self, services):
        self._services = dict(services)
        self.config = SimpleNamespace(data_dir=None)

    def has_service(self, name):
        return name in self._services

    def get_service(self, name):
        return self._services.get(name)


def _mcp_tool(enabled: bool):
    from tools.mcp.mcp_tool import MCPTool
    tool = object.__new__(MCPTool)
    tool._enabled = enabled
    return tool


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, sid="s-owner-093", public=False, services=None, correspondent=False):
        self.registry = _Registry()
        self.container = _Container(services if services is not None
                                    else {"mcp": _mcp_tool(False)})
        self.user_id = "rob"
        self.session_id = sid
        self._is_sub_agent = False
        self.orchestrator = SimpleNamespace(_public_session=public, _forged_turn_kind=None,
                                            _correspondent_session=correspondent,
                                            session_id=sid, agents={})

    def list_tools(self):
        return []

    def get_tool_load_failures(self):
        return {}

    def render_tool_catalog(self, *, is_leaf=False):
        return "<tool-catalog></tool-catalog>"


def _ctx(sid="s-owner-093", role="orchestrator", is_sub_agent=False):
    return SimpleNamespace(user_id="rob", role=role, is_sub_agent=is_sub_agent,
                           session_id=sid, metadata={})


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    return sent


def _setting_actions(c):
    from tools.controller.setting_actions import register_setting_actions
    register_setting_actions(c)
    return c.registry.actions


# --- C1: the agent reads its harness ----------------------------------------

def test_harness_view_names_the_true_reason_who_and_when():
    from tools.harness_view import tool_harness_view
    text = tool_harness_view(_Controller(), "mcp", may_propose=True)
    assert "status: gated:no-mcp-server" in text
    assert "who: owner-verb (/mcp add <id> <https-url>)" in text
    assert "when: next-turn" in text
    assert "propose_setting" in text


def test_agent_status_tool_param_returns_the_tool_view():
    from tools.controller.agent_status_action import register_agent_status_action
    c = _Controller()
    register_agent_status_action(c)
    fn, model, desc = c.registry.actions["agent_status"]
    res = asyncio.run(fn(model(tool="mcp"), execution_context=_ctx()))
    assert res.error is None
    assert "gated:no-mcp-server" in res.extracted_content
    assert "who: owner-verb" in res.extracted_content
    assert "when: next-turn" in res.extracted_content
    assert "running:" in res.extracted_content
    assert "tool" in desc


def test_a_loadable_tool_is_the_agents_own_move():
    from tools.harness_view import tool_harness_view
    text = tool_harness_view(_Controller(services={"mcp": _mcp_tool(True)}), "mcp")
    assert "status: loadable" in text and "who: agent" in text and "when: now" in text


def test_gated_block_lists_the_gated_tool_with_who_and_when():
    from tools.harness_view import gated_tools_block
    block = gated_tools_block(_Controller(), ids=["mcp"])
    assert "- mcp: gated:no-mcp-server · who=owner-verb · when=next-turn" in block
    assert gated_tools_block(_Controller(services={"mcp": _mcp_tool(True)}),
                             ids=["mcp"]) == ""


def test_a_pending_owner_deploy_is_a_status_fact(tmp_path):
    from core import lifecycle
    assert lifecycle.deploy_wait_line(tmp_path) == ""
    lifecycle.request_owner_deploy_now(data_home=tmp_path)
    line = lifecycle.deploy_wait_line(tmp_path)
    assert "owner asked for a deploy now" in line and "idle box" in line


# --- D1: the one-tap card ---------------------------------------------------

def test_propose_setting_makes_a_card_holding_the_mcp_add_line(_owner):
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    res = asyncio.run(fn(model(command="/mcp add evidence https://evidence.contact/mcp",
                               why="the 402Mob enrollment needs its MCP server"),
                         execution_context=_ctx()))
    assert res.error is None, res.error
    open_ = [c for c in cards.store().open_cards("rob") if c.kind == cards.KIND_SETTING]
    assert len(open_) == 1
    card = open_[0]
    assert card.line == "/mcp add evidence https://evidence.contact/mcp"
    text = cards.render_text(card)
    assert "network reach" in text and "untrusted" in text and "next-turn" in text
    assert cards.card_token(card.card_id, "ok") in text
    (_uid, _text, kw), = _owner
    assert kw["card_id"] == card.card_id


def test_confirm_dispatches_the_stored_line_through_the_seat(_owner):
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    asyncio.run(fn(model(command="/mcp add evidence https://evidence.contact/mcp"),
                   execution_context=_ctx()))
    card = next(c for c in cards.store().open_cards("rob") if c.kind == cards.KIND_SETTING)

    from cli.ui.commands import h_cards
    dispatched = []

    class _Seat:
        async def dispatch(self, line, ctx):
            dispatched.append(line)
            ctx.emit("Added MCP server 'evidence' (streamable_http) — "
                     "https://evidence.contact/mcp")

    ctx = SimpleNamespace(user_id="rob", emit=lambda *a, **k: None)
    import cli._admin_home as admin_home
    orig = admin_home.admin_owner_tenant
    admin_home.admin_owner_tenant = lambda uid: "rob"
    try:
        asyncio.run(h_cards._press(_Seat(), card.card_id, "ok", ctx))
    finally:
        admin_home.admin_owner_tenant = orig
    assert dispatched == ["/mcp add evidence https://evidence.contact/mcp"]
    after = cards.store().get(card.card_id)
    assert after.state == cards.S_DONE
    # A second tap runs nothing.
    p = cards.press(card.card_id, "ok", "rob")
    assert p.run is None


def test_tap_line_names_the_setting_line():
    card = cards.setting_card("rob", ["/config", "set", "style.tone", "friendly"],
                              title="Proposed setting")
    assert cards.tap_line(cards.card_token(card.card_id, "ok"), "rob") == \
        "/config set style.tone friendly"


def test_setting_card_refuses_other_shapes():
    for words in (["/send", "1", "native", "to", "0xabc"], ["/mcp", "remove", "x"],
                  ["/job", "edit", "abc", "schedule", "daily"],
                  ["/config", "set", "style.tone", "go"]):
        with pytest.raises(ValueError):
            cards.setting_card("rob", words, title="x")


# --- D4: owner turn only ----------------------------------------------------

def test_an_autonomous_run_is_refused(_owner):
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("s-auto-093", goal_id="g1")
    c = _Controller(sid="s-auto-093")
    fn, model, _ = _setting_actions(c)["propose_setting"]
    res = asyncio.run(fn(model(command="/mcp add evidence https://evidence.contact/mcp"),
                         execution_context=_ctx(sid="s-auto-093")))
    assert res.error and "owner turn" in res.error
    assert not [c for c in cards.store().open_cards("rob") if c.kind == cards.KIND_SETTING]
    assert _owner == []


def test_a_correspondent_session_is_refused(monkeypatch, _owner):
    monkeypatch.setattr("agents.task.session_class.correspondent_facing", lambda orch: True)
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    res = asyncio.run(fn(model(command="/mcp add evidence https://evidence.contact/mcp"),
                         execution_context=_ctx()))
    assert res.error and "correspondent" in res.error
    assert _owner == []


def test_a_room_and_a_leaf_are_refused(_owner):
    fn, model, _ = _setting_actions(_Controller(public=True))["propose_setting"]
    res = asyncio.run(fn(model(command="/mcp add e https://e.example/mcp"),
                         execution_context=_ctx()))
    assert res.error and "room" in res.error
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    res = asyncio.run(fn(model(command="/mcp add e https://e.example/mcp"),
                         execution_context=_ctx(role="leaf", is_sub_agent=True)))
    assert res.error and "leaf" in res.error


# --- refusals and the operator path -----------------------------------------

@pytest.mark.parametrize("command", [
    "/config set budget.wallet_daily_usd 50",
    "/config set WALLET_DAILY_CAP_USD 50",
    "/goal grant abc123 tools=defi_trade",
    "/send 1 native to 0xabc on base",
    "/mcp add evidence http://insecure.example/mcp",
    "/mcp add evidence https://e.example/mcp sk-secret-key",
])
def test_money_and_unknown_shapes_are_refused(command, _owner):
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    res = asyncio.run(fn(model(command=command), execution_context=_ctx()))
    assert res.error and "refused" in res.error
    assert not [c for c in cards.store().open_cards("rob") if c.kind == cards.KIND_SETTING]


def test_an_operator_setting_is_filed_not_carded(monkeypatch, _owner):
    made = []

    class _Board:
        def asks(self, **kw):
            return []

        def create_ask(self, **kw):
            made.append(kw)
            return SimpleNamespace(id="abcdef123456")

    monkeypatch.setattr("tools.controller.owner_ask_action._board", lambda c: _Board())
    fn, model, _ = _setting_actions(_Controller())["propose_setting"]
    res = asyncio.run(fn(model(command="/config set CODE_EXEC_BACKEND docker",
                               why="shell cannot start"), execution_context=_ctx()))
    assert res.error is None
    assert "#abcdef12" in res.extracted_content and "needs the operator" in res.extracted_content
    (kw,) = made
    assert kw["extra_payload"]["ask_kind"] == "config_change"
    assert kw["extra_payload"]["line"] == "CODE_EXEC_BACKEND=docker"
    assert kw["force"] is True   # the exact-line dedupe, never the fuzzy title one
    assert not [c for c in cards.store().open_cards("rob") if c.kind == cards.KIND_SETTING]


def test_a_member_flag_is_filed_with_its_overrides_line():
    from core.setting_proposals import OPERATOR, classify
    p = classify("/config set MCP_ENABLED true")
    assert p.outcome == OPERATOR
    assert p.env_line == "POLYROB_OVERRIDES=MCP_ENABLED=true"


def test_a_safe_preference_and_a_non_money_grant_are_cards():
    from core.setting_proposals import CARD, classify
    p = classify("/config set style.tone friendly")
    assert p.outcome == CARD and p.when == "next-turn"
    g = classify("/job edit abc123 grant tools=web_fetch")
    assert g.outcome == CARD and g.words[:4] == ["/job", "edit", "abc123", "grant"]


def test_the_owner_prompt_teaches_the_five_fields():
    from agents.task.agent.prompts import SystemPrompt
    dm = {"surface_id": "telegram", "chat_type": "dm"}
    text = SystemPrompt("actions", use_native_tools=True, tool_ids=[],
                        surface=dm).get_system_message().content
    assert 'agent_status(tool="<id>")' in text and "propose_setting" in text
    auto = SystemPrompt("actions", use_native_tools=True, tool_ids=[],
                        autonomous=True).get_system_message().content
    assert "propose_setting" not in auto
