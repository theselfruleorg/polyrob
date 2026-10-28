"""093 review fixes — one regression test per verified finding.

Settings cards (``core/setting_proposals.py``, ``tools/controller/setting_actions.py``),
the harness view, the remedy table, the owner-seat pin, the retry wall, the
wallet_sign hint and the ``/update now`` marker.
"""
import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from core.setting_proposals import CARD, OPERATOR, REFUSED, classify


# --- 1: an operator change is deduped on its EXACT line ------------------------

class _Board:
    """The goal board's ask surface, with its real fuzzy-title merge."""

    def __init__(self):
        self.rows = []

    def asks(self, *, user_id, status):
        return [a for a in self.rows if a.status == status]

    def create_ask(self, *, user_id, what, why="", extra_payload=None, force=False, **kw):
        from agents.task.goals.board_asks import title_similarity
        if not force:
            for a in self.rows:
                if title_similarity(what, a.title) >= 0.6:
                    return a          # the fuzzy merge that dropped the new line
        a = SimpleNamespace(id=f"ask{len(self.rows):09d}", title=what, status="open",
                            payload=dict(extra_payload or {}))
        self.rows.append(a)
        return a


def _file(board, line_cmd, monkeypatch):
    from tools.controller import setting_actions as sa
    monkeypatch.setattr("tools.controller.owner_ask_action._board", lambda c: board)
    p = classify(line_cmd)
    assert p.outcome == OPERATOR, p.reason
    return asyncio.run(sa._file_for_operator(SimpleNamespace(), "rob", "s1", p, "why"))


def test_two_different_operator_lines_are_two_asks(monkeypatch):
    board = _Board()
    first = _file(board, "/config set POLYROB_OVERRIDES MCP_ENABLED=true", monkeypatch)
    second = _file(board, "/config set POLYROB_OVERRIDES CRON_ENABLED=true", monkeypatch)
    assert len(board.rows) == 2
    assert {a.payload["line"] for a in board.rows} == {
        "POLYROB_OVERRIDES=MCP_ENABLED=true", "POLYROB_OVERRIDES=CRON_ENABLED=true"}
    assert first.error is None and second.error is None
    assert board.rows[0].id != board.rows[1].id


def test_the_same_operator_line_is_one_ask(monkeypatch):
    board = _Board()
    _file(board, "/config set CODE_EXEC_BACKEND docker", monkeypatch)
    _file(board, "/config set CODE_EXEC_BACKEND docker", monkeypatch)
    assert len(board.rows) == 1


# --- 2: POLYROB_OVERRIDES members and raise-only flags --------------------------

@pytest.mark.parametrize("command", [
    "/config set POLYROB_OVERRIDES X402_ENABLED=true,WALLET_ENABLED=true",
    "/config set POLYROB_OVERRIDES MCP_ENABLED=true,X402_ENABLED=true",
    "/config set POLYROB_OVERRIDES MCP_ENABLED=true,AUTONOMY_HALT=false",
    "/config set POLYROB_OVERRIDES MCP_ENABLED",
    "/config set POLYROB_OVERRIDES NOT_A_REAL_FLAG_093=true",
    "/config set AUTONOMY_HALT 0",
    "/config set AUTONOMY_HALT false",
])
def test_overrides_members_and_a_cleared_safety_stop_are_refused(command):
    p = classify(command)
    assert p.outcome == REFUSED, (command, p)
    assert not p.env_line


def test_a_safe_overrides_line_is_still_filed_and_a_halt_is_a_card():
    p = classify("/config set POLYROB_OVERRIDES MCP_ENABLED=true")
    assert p.outcome == OPERATOR and p.env_line == "POLYROB_OVERRIDES=MCP_ENABLED=true"
    assert classify("/config set AUTONOMY_HALT 1").outcome == CARD


# --- 3: exposure flags refused; self-writing grants refused; exec risk -----------

@pytest.mark.parametrize("key,value", [
    ("AGENT_BUILDER_MODE", "ship"), ("KB_API_ENABLED", "true"),
    ("OPENAI_COMPAT_API_ENABLED", "true"), ("CRON_WRITE_JOBS_ENABLED", "true"),
    ("WEBVIEW_PORT", "80"), ("UVICORN_WORKERS", "4"),
])
def test_ingress_and_exposure_settings_are_refused(key, value):
    p = classify(f"/config set {key} {value}")
    assert p.outcome == REFUSED and "exposure" in p.reason


def test_a_self_writing_tool_grant_is_refused_and_exec_carries_its_risk():
    assert classify("/goal grant abc123 tools=self_env").outcome == REFUSED
    p = classify("/goal grant abc123 tools=shell")
    assert p.outcome == CARD
    assert "commands or code on this host" in p.risk and "shell" in p.risk
    assert "on this host" not in classify("/goal grant abc123 tools=web_fetch").risk


# --- 4: the MCP URL the owner reads is where the card connects ------------------

@pytest.mark.parametrize("url", [
    "https://github.com@attacker.example/mcp",
    "https://user:pw@mcp.example/mcp",
    "https://mcp.example/mcp?api_key=sk-123",
    "https://mcp.example/mcp#frag",
    "https://exаmple.com/mcp",              # Cyrillic a
    "https://mcp.example:99999/mcp",
])
def test_an_mcp_url_with_userinfo_query_or_lookalike_host_is_refused(url):
    assert classify(f"/mcp add srv {url}").outcome == REFUSED


def test_the_mcp_card_names_the_parsed_host():
    p = classify("/mcp add srv https://mcp.example/v1/mcp")
    assert p.outcome == CARD and "(host: mcp.example)" in p.change


# --- 5: a grant card says what it replaces --------------------------------------

def test_a_grant_card_names_the_tools_it_replaces(tmp_path):
    from core.runtime_paths import cron_db_path, goals_db_path
    from tools.controller.setting_actions import _current_tools
    gdb = goals_db_path(str(tmp_path))
    con = sqlite3.connect(gdb)
    con.execute("CREATE TABLE IF NOT EXISTS goals (id TEXT, user_id TEXT, kind TEXT, payload TEXT)")
    con.execute("INSERT INTO goals VALUES ('abc123ff', 'rob', 'goal', ?)",
                (json.dumps({"tools": ["email", "browser"]}),))
    con.commit(); con.close()
    ctl = SimpleNamespace(container=SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    import core.runtime_paths as rp
    orig = rp.container_data_home
    rp.container_data_home = lambda c: str(tmp_path)
    try:
        assert _current_tools(ctl, "rob", "/goal", "abc123") == ["email", "browser"]
        assert _current_tools(ctl, "other", "/goal", "abc123") is None
        assert _current_tools(ctl, "rob", "/job", "abc123") is None   # no cron db
    finally:
        rp.container_data_home = orig
    assert cron_db_path(str(tmp_path))

    from core.setting_proposals import with_current_tools
    p = classify("/goal grant abc123 tools=web_fetch")
    assert "replaces its current tools: email, browser" in with_current_tools(
        p, ["email", "browser"]).change
    assert "could not be read" in with_current_tools(p, None).change
    assert "no tools now" in with_current_tools(p, []).change
    assert with_current_tools(p, ["x"]).words == p.words   # the verb is unchanged


# --- 6: the ask kind lives with the board's literals ----------------------------

def test_config_change_kind_is_a_board_literal_not_an_own_surface_kind():
    from core import goal_vocab
    from tools.controller import setting_actions
    assert goal_vocab.ASK_KIND_CONFIG_CHANGE == "config_change"
    assert setting_actions.ASK_KIND_CONFIG_CHANGE is goal_vocab.ASK_KIND_CONFIG_CHANGE
    assert "config_change" not in goal_vocab.OWN_SURFACE_ASK_KINDS


# --- 7: the view never suggests a refused propose_setting ------------------------

def _ctl(sid="s-review-093"):
    from tests.unit.tools.test_093_harness_and_settings import _Controller
    return _Controller(sid=sid)


def _ctx(sid="s-review-093", **kw):
    base = dict(user_id="rob", role="orchestrator", is_sub_agent=False, session_id=sid,
                metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")


def test_the_view_suggests_propose_setting_only_on_an_owner_turn(_owner):
    from tools.controller.agent_status_action import _may_propose
    from tools.controller.setting_actions import register_setting_actions
    from tools.harness_view import tool_harness_view

    def ctl(sid="s-review-093"):
        c = _ctl(sid)
        register_setting_actions(c)
        return c

    assert _may_propose(_ctl(), _ctx()) is False      # no propose_setting registered
    assert _may_propose(ctl(), _ctx()) is True
    assert _may_propose(ctl(), None) is False                       # no turn known
    assert _may_propose(ctl(), _ctx(role="leaf", is_sub_agent=True)) is False
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("s-auto-review-093", goal_id="g1")
    assert _may_propose(ctl("s-auto-review-093"), _ctx("s-auto-review-093")) is False
    from tools import harness_view as hv
    st = SimpleNamespace(tool_id="mcp", status="gated", reason="no-mcp-server",
                         remedy="the owner adds one with /mcp add <id> <https-url>")
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(hv, "status_of", lambda *a, **k: st)
    try:
        assert "propose_setting" in tool_harness_view(_ctl(), "mcp", may_propose=True)
        assert "propose_setting" not in tool_harness_view(_ctl(), "mcp")
    finally:
        mp.undo()


def test_the_view_never_suggests_propose_setting_for_a_money_gate(monkeypatch):
    from tools import harness_view as hv
    st = SimpleNamespace(tool_id="defi_trade", status="gated", reason="money",
                         remedy="money tools are explicit-grant-only")
    monkeypatch.setattr(hv, "status_of", lambda *a, **k: st)
    text = hv.tool_harness_view(_ctl(), "defi_trade", may_propose=True)
    assert "gated:money" in text and "propose_setting" not in text


# --- 8: the who word of a remedy agrees with its row -----------------------------

def test_mcp_servers_names_the_line_that_works():
    from core.tool_gates import REMEDIES, REMEDY_ACTORS
    assert REMEDY_ACTORS["mcp_servers"].verb == "POLYROB_OVERRIDES=MCP_ENABLED=true"
    assert "/mcp add <id> <https-url>" in REMEDIES["mcp_servers"]
    assert "as the agent's user" in REMEDIES["backend-unavailable"]


def test_every_remedy_text_names_the_actor_its_row_names():
    from core.tool_gates import REMEDIES, REMEDY_ACTORS
    for key, text in REMEDIES.items():
        who = REMEDY_ACTORS[key].who
        if text.startswith("owner "):
            assert who == "owner-verb", (key, who)
        if text.startswith("operator "):
            assert who == "operator-env", (key, who)
        if who == "operator-env":
            assert not text.startswith("owner "), key


def test_every_operator_env_flag_named_by_a_remedy_is_not_seat_writable():
    """The truth behind the actor word: a seat's /config set refuses each flag."""
    from core.setting_proposals import classify as c
    for key in ("KB_ENABLED", "GOALS_ENABLED", "CODING_TOOLS_ENABLED", "CRON_ENABLED",
                "MCP_ENABLED", "AGENT_COMPUTE_POSTURE", "X402_INVOICE_ENABLED"):
        assert c(f"/config set {key} true").outcome != CARD, key


# --- 9: the catalog row ----------------------------------------------------------

def test_mcp_gate_label_and_http_request_is_leaf_blocked():
    from core.tool_capabilities import TOOL_CAPABILITIES, row_field
    assert row_field("mcp", "gate").label == "MCP_ENABLED + an MCP server"
    assert "delegate_blocked" in TOOL_CAPABILITIES["http_request"]
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
    assert "http_request" in DELEGATE_BLOCKED_TOOLS


# --- 10: a leaf never pins the owner manual --------------------------------------

def test_a_leaf_in_an_owner_session_pins_no_manual():
    from agents.task.agent.core import construction as c
    from core import autonomy_marker as m
    m.mark_owner_seat("sess-owner-review-093")
    orch = SimpleNamespace(session_id="sess-owner-review-093")
    assert c.owner_seat_skill_ids(SimpleNamespace(orchestrator=orch)) == ["harness-manual"]
    assert c.owner_seat_skill_ids(SimpleNamespace(orchestrator=orch, _is_sub_agent=True)) == []
    assert c.owner_seat_skill_ids(SimpleNamespace(orchestrator=orch, _role="leaf")) == []


# --- 12: the MCP-off prompt part names the owner's verb on an owner seat ---------

def test_mcp_off_tells_an_owner_seat_the_verb_and_nobody_else():
    from agents.task.agent.prompts import SystemPrompt
    owner = SystemPrompt("actions", use_native_tools=True, tool_ids=["web_fetch"],
                         owner_seat=True)._get_mcp_section()
    other = SystemPrompt("actions", use_native_tools=True,
                         tool_ids=["web_fetch"])._get_mcp_section()
    assert "/mcp add <id> <https-url>" in owner
    assert "/mcp add" not in other


# --- 13: a REPL line resets the wall; the wall says what resets it ---------------

def test_a_repl_line_resets_the_retry_wall():
    from unittest.mock import AsyncMock, MagicMock
    from agents.task.agent.conversation import Conversation
    agent = MagicMock()
    agent._retry_wall_steps = 2
    agent.run = AsyncMock(return_value=[])
    agent.state = SimpleNamespace(consecutive_failures=0)
    agent._cancelled = False
    asyncio.run(Conversation(agent).respond("we fixed it, try again"))
    agent.controller.reset_retry_wall.assert_called_once()
    assert agent._retry_wall_steps == 0


def test_the_wall_text_names_only_what_resets_it():
    from tools.controller.execution import _retry_wall_text
    text = _retry_wall_text("x", 3, "boom")
    assert "or a new run" not in text
    assert "owner's next message" in text


# --- 15: the wallet_sign hint never carries a withheld secret --------------------

def test_submit_urls_skip_secret_keys_and_strip_queries():
    from tools.wallet_sign.tool import _submit_urls
    challenge = {
        "access_token_url": "https://site.example/cb?access_token=SECRET",
        "auth": {"callback": "https://site.example/auth/cb"},
        "submit_url": "/api/verify?session=abc#top",
        "links": ["https://u:p@site.example/enroll?x=1"],
    }
    urls = _submit_urls(challenge, "site.example")
    assert urls == ["https://site.example/api/verify", "https://site.example/enroll"]
    assert not any("SECRET" in u or "?" in u or "#" in u or "@" in u for u in urls)


# --- 16: the /update now marker is bounded by OUR TTL ----------------------------

def test_a_marker_older_than_the_ttl_is_dead_whatever_expires_at_says(tmp_path):
    from core import lifecycle as lc
    rec = lc.request_owner_deploy_now(data_home=tmp_path)
    assert lc.read_owner_deploy_now(data_home=tmp_path) is not None
    old = time.time() - lc.OWNER_DEPLOY_NOW_TTL_SEC - 5
    rec.update(at=old, expires_at=time.time() + 10 * 86400)
    (tmp_path / lc.OWNER_DEPLOY_NOW_FILE).write_text(json.dumps(rec))
    assert lc.read_owner_deploy_now(data_home=tmp_path) is None
    assert lc.deploy_wait_line(tmp_path) == ""
