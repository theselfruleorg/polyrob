"""Proposal 001: goal_create accepts a `tools` list, filtered to a safe allowlist.

Owner-approved option 2 (2026-07-01): Rob's self-created goals may request research/content/coding
tools AND twitter, but never money (wallet/x402/hyperliquid/polymarket), code execution, cron, or
meta goal/skill tools.
"""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalTool, GoalCreateAction, _SELF_GOAL_ALLOWED_TOOLS


class _Ctx:
    user_id = "tester"


def _make_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)  # skip BaseTool.__init__ (needs a full container)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


def test_allowlist_shape():
    for safe in ("filesystem", "task", "coding", "web_fetch", "twitter"):
        assert safe in _SELF_GOAL_ALLOWED_TOOLS
    for danger in ("x402_pay", "hyperliquid", "polymarket", "code_execution", "cronjob", "goal", "wallet"):
        assert danger not in _SELF_GOAL_ALLOWED_TOOLS


def test_spend_money_tools_excluded_from_every_self_goal_grant(monkeypatch):
    """Injected-goal laundering ratchet: an agent-created goal must never be able
    to acquire a money-SPEND verb. The turn-origin gate treats a genuine
    autonomous goal turn as allowed, so this tool-grant allowlist is the ONLY
    line stopping an injected goal from trading. Pin the whole SPEND set (derived
    from the capability table, so a future money tool is covered too) out of
    every grant surface, in BOTH autonomy modes.
    """
    from core.tool_capabilities import ids_with
    from tools.goal_tools import allowed_self_goal_tools, _SELF_GOAL_ALLOWED_TOOLS
    from agents.task.goals.dispatcher import (
        CHILD_INHERITABLE_TOOLS, default_goal_tools, child_inheritable_tools)
    from agents.task.constants import AUTONOMOUS_MODE_TOOLS

    # x402_invoice is RECEIVABLES (create an invoice), deliberately allowed; the
    # SPEND verbs are what must never be self-grantable.
    spend = set(ids_with("money")) - {"x402_invoice"}
    assert "defi_trade" in spend  # the tool this ratchet was written for

    for mode_on in (False, True):
        monkeypatch.setattr("core.config_policy.full_autonomy_enabled",
                            lambda: mode_on, raising=False)
        monkeypatch.setattr("agents.task.constants.full_autonomy_enabled",
                            lambda: mode_on, raising=False)
        assert not (spend & set(allowed_self_goal_tools())), mode_on
        assert not (spend & set(default_goal_tools())), mode_on
        assert not (spend & set(child_inheritable_tools())), mode_on

    assert not (spend & set(_SELF_GOAL_ALLOWED_TOOLS))
    assert not (spend & set(AUTONOMOUS_MODE_TOOLS))
    assert not (spend & set(CHILD_INHERITABLE_TOOLS))


def test_goal_create_keeps_allowlisted_drops_blocked(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest goal", body="b",
                         tools=["coding", "twitter", "x402_pay", "cronjob", "wallet"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    # Assert on the GRANTED toolset segment, not the whole message: since 029 R5
    # the result also NAMES what was dropped, so a blocked id legitimately
    # appears in the text — as a refusal, which is the point.
    toolset = txt.split("tools=", 1)[1].split(":", 1)[0]
    assert "coding" in toolset and "twitter" in toolset
    for danger in ("x402_pay", "cronjob", "wallet"):
        assert danger not in toolset
    assert "x402_pay" in txt, "the drop must be reported, not silent"


def test_goal_create_without_tools_has_no_toolset(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(GoalCreateAction(title="captest goal 2", body="b"), _Ctx()))
    assert "tools=" not in res.extracted_content


# --- Proposal 009 (2026-07-14, battle-test night-1): mission tools + text inference ---

def test_allowlist_includes_kickoff_mission_tools():
    """Owner kickoff (2026-07-13) sanctions email outreach, telegram posting and
    x402 invoicing for self-created goals; spend-side stays excluded."""
    for mission in ("email", "message", "x402_invoice", "knowledge"):
        assert mission in _SELF_GOAL_ALLOWED_TOOLS
    for danger in ("x402_pay", "hyperliquid", "polymarket", "code_execution", "cronjob", "goal", "wallet"):
        assert danger not in _SELF_GOAL_ALLOWED_TOOLS


def test_goal_create_infers_tools_from_text_when_unset(tmp_path):
    """A goal whose text names a capability gets that tool + the safe baseline —
    the exact night-1 failure ('Publish queued OSS launch X thread' dispatched
    without twitter)."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="Publish queued OSS launch X thread",
                         body="post the tweet thread", acceptance="live tweet url"),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "twitter" in txt
    # baseline rides along so the session isn't starved of basics
    assert "filesystem" in txt and "task" in txt and "web_fetch" in txt


def test_goal_create_inference_covers_mission_surfaces(tmp_path):
    tool = _make_tool(tmp_path)
    cases = {
        "Use rob mailbox for registrations": ("send email signups", "email"),
        "Find x402 services and earn": ("issue an invoice for value", "x402_invoice"),
        "Introduce yourself in t.me/thepublicden": ("post in the telegram group", "message"),
        "Re-learn POLYROB docs": ("fetch polyrob.dev and update notes", "web_fetch"),
    }
    for title, (body, expected) in cases.items():
        res = asyncio.run(tool.goal_create(GoalCreateAction(title=title, body=body), _Ctx()))
        assert expected in res.extracted_content, (title, res.extracted_content)


def test_goal_create_explicit_tools_get_baseline_union(tmp_path):
    """Explicit tools=['twitter'] must not strand the session without basics."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest explicit union", body="b", tools=["twitter"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "twitter" in txt and "filesystem" in txt and "task" in txt


# --- S4 (dynamic tool rig, 2026-07-20): create-time inference stops NARROWING ---

def test_goal_create_no_inferred_narrowing_under_tool_disclosure(tmp_path, monkeypatch):
    """With TOOL_PROGRESSIVE_DISCLOSURE on, an inference-only goal stays tools-less:
    payload.tools would short-circuit dispatch's WIDE autonomous default, and the
    S1 catalog + load_tool make create-time keyword guesses obsolete (dispatch-time
    inference remains, as a widening hint)."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="Publish queued OSS launch X thread",
                         body="post the tweet thread", acceptance="live tweet url"),
        _Ctx(),
    ))
    assert "tools=" not in res.extracted_content


def test_goal_create_explicit_tools_still_written_under_tool_disclosure(tmp_path, monkeypatch):
    """Explicit tools remain a deliberate narrowing/grant — unchanged by the flag."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest explicit under disclosure", body="b",
                         tools=["twitter"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "twitter" in txt and "filesystem" in txt


# --- T2.1 Task 4: depends_on through goal_create ----------------------------

def test_goal_create_action_accepts_depends_on():
    action = GoalCreateAction(title="a valid dependent title", depends_on=["g1", "g2"])
    assert action.depends_on == ["g1", "g2"]


def test_goal_create_action_without_depends_on_defaults_none():
    action = GoalCreateAction(title="a valid title, no deps")
    assert action.depends_on is None


def test_goal_create_action_still_forbids_unknown_fields():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GoalCreateAction(title="a valid title", depends_on=["g1"], bogus_field="nope")


def test_goal_create_via_tool_with_open_dep_lands_waiting(tmp_path):
    tool = _make_tool(tmp_path)
    board = tool._goal_board
    dep = board.create(user_id="tester", title="prerequisite goal entirely")
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="dependent goal entirely", body="b", depends_on=[dep.id]),
        _Ctx(),
    ))
    assert not res.error
    assert "status=waiting" in res.extracted_content
    dependent = next(g for g in board.list(user_id="tester") if g.title == "dependent goal entirely")
    assert dependent.status == "waiting"
    assert board.dependencies(dependent.id) == [dep.id]


def test_goal_create_via_tool_cross_tenant_dep_errors_no_orphan_row(tmp_path):
    tool = _make_tool(tmp_path)
    board = tool._goal_board
    other = board.create(user_id="someone-else", title="not testers goal at all")
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="dependent goal blocked by other tenant", body="b",
                         depends_on=[other.id]),
        _Ctx(),
    ))
    assert res.error
    assert "Cannot create goal" in res.error
    # all-or-nothing: board.create validates BEFORE the row is written, so the
    # rejected create must leave zero orphan rows for this tenant.
    assert board.list(user_id="tester") == []


def test_goal_create_inference_never_grants_money_spend(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest pay wallet hyperliquid x402_pay goal",
                         body="x402_pay wallet hyperliquid polymarket cron"),
        _Ctx(),
    ))
    txt = res.extracted_content
    # assert on the granted toolset segment, not the echoed title
    assert "tools=" in txt
    toolset = txt.split("tools=", 1)[1].split(":", 1)[0]
    for danger in ("x402_pay", "wallet", "hyperliquid", "polymarket", "cronjob"):
        assert danger not in toolset
    # ("x402" token in the text legitimately infers the capped x402_invoice receivable tool)
    assert "x402_invoice" in toolset


# --- 029 R5: a silent strip is why the agent kept retrying --------------------
#
# The filter is correct and stays. What was wrong is that it was SILENT: dropped
# tools went to a log line the agent never sees, so goal_create returned success
# and the agent learned nothing. It then discovered the gap at dispatch, filed
# "defi_trade not granted" as an owner ask, and repeated - ~50 times in two
# weeks of prod. A boundary the caller cannot see is one it cannot respect.

def test_dropped_tools_are_reported_back_not_only_logged(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest dropped tools are visible", body="b",
                         tools=["coding", "cronjob"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    assert "cronjob" in txt, "the agent must be told what it did not get"
    assert "coding" in txt


def test_a_dropped_money_tool_says_it_can_never_be_granted_this_way(tmp_path):
    """The fact that ends the retry loop. Not 'ask again with better wording'."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest money tool drop is explained", body="b",
                         tools=["defi_trade", "filesystem"]),
        _Ctx(),
    ))
    txt = res.extracted_content.lower()
    assert "defi_trade" in txt
    assert "never" in txt
    assert "owner" in txt or "operator" in txt


def test_the_money_tool_is_still_actually_stripped(tmp_path):
    """The message changes; the boundary does not."""
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest money stays stripped entirely", body="b",
                         tools=["defi_trade", "filesystem"]),
        _Ctx(),
    ))
    txt = res.extracted_content
    toolset = txt.split("tools=", 1)[1].split(":", 1)[0]
    assert "defi_trade" not in toolset
    assert "filesystem" in toolset


def test_nothing_dropped_means_no_noise(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest clean grant no noise", body="b",
                         tools=["coding"]),
        _Ctx(),
    ))
    assert "not granted" not in res.extracted_content.lower()


# --- 029 R5: the READ-only DeFi tool belongs in the self-goal allowlist -------

def test_defi_data_is_self_grantable_but_defi_trade_is_not():
    """defi_data constructs no signer and broadcasts nothing - it is token
    SIGHT. Excluding it forced every screening goal to wait for an
    operator-seeded cycle, which is a tax on reconnaissance, not a safety
    property. The money verb stays excluded; that is where the line is."""
    allowed = _SELF_GOAL_ALLOWED_TOOLS
    assert "defi_data" in allowed
    assert "defi_trade" not in allowed


def test_adding_defi_data_did_not_smuggle_in_a_money_capability():
    from core.tool_capabilities import ids_with
    assert "defi_data" not in set(ids_with("money"))


def test_a_self_created_screening_goal_can_carry_defi_data(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="captest screen fresh base launches today",
                         body="screen new pools", tools=["defi_data"]),
        _Ctx(),
    ))
    toolset = res.extracted_content.split("tools=", 1)[1].split(":", 1)[0]
    assert "defi_data" in toolset
