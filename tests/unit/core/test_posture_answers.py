"""104: one answer to the four questions, derived from the card and never a promise."""
import pytest

import agents.task.constants as constants
from core.config_policy import posture_card as pc


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in ("POLYROB_LOCAL", "AUTONOMY_ENABLED", "AUTONOMY_MODE", "AUTONOMY_POSTURE",
              "AGENT_COMPUTE_POSTURE", "APPROVAL_REQUIRED_TOOLS", "AUTONOMY_HALT",
              "DEFI_AGENT_AUTONOMY", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    constants._refreeze_compute_posture_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()


def _text(seat=""):
    return "\n".join(pc.build_answers(seat))


def test_a_new_install_says_the_edits_run_without_asking():
    """The walk: edits ran with no prompt. The answer must not claim "asks first"."""
    text = _text("terminal")
    assert "change files in its work folder" in text
    assert "Asks first: nothing else is set to ask." in text
    assert "When you are away: nothing runs until you write." in text
    assert "Money: only when you confirm it." in text
    assert "Commands on this computer: no." in text


def test_a_required_file_action_moves_out_of_without_asking(monkeypatch):
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "coding_create_file")
    monkeypatch.setenv("APPROVAL_PROVIDER", "interactive_cli")
    text = _text()
    assert "change files" not in text
    assert "Asks first: coding_create_file." in text


def test_a_list_under_the_auto_provider_does_not_read_as_asking(monkeypatch):
    """The walk: the list alone was auto-approved, with no question."""
    monkeypatch.setenv("APPROVAL_REQUIRED_TOOLS", "coding_create_file")
    text = _text()
    assert "change files in its work folder" in text
    assert "`coding_create_file` is listed, but it runs with no question" in text


@pytest.mark.parametrize("env,compute,want", [
    ({}, "0", "auto"),
    ({}, "2", "interactive_cli"),
    ({"APPROVAL_PROVIDER": "deny"}, "2", "deny"),
    ({"APPROVAL_PROVIDER": "interactive_cli"}, "0", "interactive_cli"),
])
def test_the_provider_read_matches_the_controller(monkeypatch, env, compute, want):
    import tools.controller.approval as approval
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", compute)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    constants._refreeze_compute_posture_for_tests()
    monkeypatch.setattr(approval, "_FROZEN_APPROVAL_PROVIDER", env.get("APPROVAL_PROVIDER", "auto"))
    assert pc.effective_approval_provider() == want == approval.resolve_gated_actions()[1]


def test_compute_two_lists_the_recommended_set_and_the_shell(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    constants._refreeze_compute_posture_for_tests()
    asks = pc.asks_first_actions()
    assert {"git_push", "shell_run"} <= set(asks)
    text = _text()
    assert "git_push" in text
    assert "Commands: only in a sandbox, and it asks first about a risky one." in text


def test_a_store_fault_is_not_an_empty_list(monkeypatch):
    def boom(*a, **k):
        raise OSError("unreadable")
    monkeypatch.setattr("core.prefs.resolve", boom)
    text = _text()
    assert "Asks first: unavailable(OSError)." in text
    assert "nothing else is set to ask" not in text


@pytest.mark.parametrize("posture,word", [("silent", "only when a goal ends"),
                                          ("owner-visible", "results and blockers")])
def test_away_reads_the_loop_setting(monkeypatch, posture, word):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("AUTONOMY_ENABLED", "true")
    monkeypatch.setenv("AUTONOMY_POSTURE", posture)
    assert word in _text()


@pytest.mark.parametrize("seat,right,wrong", [
    ("terminal", "polyrob config set", "computer it runs on"),
    ("telegram", "computer it runs on", "polyrob config set"),
    ("console", "Agent > Settings", "/config set"),
])
def test_the_change_line_is_true_on_each_seat(seat, right, wrong):
    text = _text(seat)
    assert right in text and wrong not in text


def test_a_seatless_read_has_no_who_or_change_line():
    text = _text()
    assert "You are the owner" not in text and "To change" not in text


def test_a_console_process_sends_a_terminal_prompt_to_the_inbox(monkeypatch):
    """The walk: the prompt reached the hidden terminal and the browser waited 30 s."""
    import tools.controller.approval as approval
    monkeypatch.setattr(approval, "_FROZEN_APPROVAL_PROVIDER", "interactive_cli")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    assert approval.effective_approval_state("owner-1", None)[1] == "interactive_cli"
    monkeypatch.setattr(approval, "_CONSOLE_PROCESS", True)
    assert approval.effective_approval_state("owner-1", None)[1] == "owner_queue"
    # A tenant on a served console keeps the fail-closed denial.
    assert approval.effective_approval_state("tenant-9", None)[1] == "interactive_cli"


def test_a_console_chat_send_is_not_routed_as_a_lost_message(monkeypatch):
    """The walk: each reply the console showed counted as 'reached nobody' in Health."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import tools.controller.approval as approval
    from tools.controller import send_message_route as route
    deliver = AsyncMock(return_value="no_sink")
    monkeypatch.setattr("core.surfaces.user_delivery.maybe_deliver_autonomous_send", deliver)
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda s: False)
    controller = MagicMock(session_id="s1")
    monkeypatch.setattr(approval, "_CONSOLE_PROCESS", True)
    assert asyncio.run(route.route_autonomous_send(controller, "hi")) is None
    deliver.assert_not_called()
    monkeypatch.setattr(approval, "_CONSOLE_PROCESS", False)
    assert asyncio.run(route.route_autonomous_send(controller, "hi")) == "no_sink"


def test_a_refused_load_reads_as_refused():
    from agents.task.telemetry.narrate import narrate
    from core.copy import t
    event = {"tool_name": "default", "action_name": "load_tool", "success": True,
             "result_preview": "load_tool refused (gated:money): tool 'x402_pay'"}
    assert narrate(event) == t("chat.act.refused")


@pytest.mark.parametrize("creator,want", [("cli", True), ("api", False), ("", False)])
def test_only_a_terminal_chat_stays_in_the_terminal(monkeypatch, creator, want):
    from webview import chat_open
    monkeypatch.setattr(chat_open, "first_line_context", lambda s: {"creator": creator})
    assert chat_open.is_terminal_chat("s1") is want


@pytest.mark.parametrize("key,refused", [
    ("AUTONOMY_POSTURE", False), ("AUTONOMY_ENABLED", False),  # D5: how visible, and on or off
    ("AUTONOMY_MODE", True), ("AGENT_COMPUTE_POSTURE", True), ("POLYROB_LOCAL", True),
    ("APPROVAL_REQUIRED_TOOLS", True), ("APPROVAL_PROVIDER", True), ("WALLET_DAILY_CAP_USD", True),
])
def test_what_a_remote_seat_may_change(key, refused):
    from core.config_service import remote_write_refused
    assert remote_write_refused(key, "x") is refused


def test_a_remote_refusal_names_the_next_step(tmp_path, monkeypatch):
    from core.config_service import set_value
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    res = set_value("AUTONOMY_MODE", "autonomous", surface="telegram")
    assert res.outcome == "refused"
    assert "`polyrob config set AUTONOMY_MODE <value>`" in res.message


@pytest.mark.parametrize("env,right,wrong", [
    # The walk of the rules: scheduled jobs need `full`, goals need the local profile.
    ({"POLYROB_LOCAL": "1", "AUTONOMY_ENABLED": "true", "AUTONOMY_POSTURE": "owner-visible"},
     "goals run", "scheduled jobs"),
    ({"POLYROB_LOCAL": "1", "AUTONOMY_ENABLED": "true", "AUTONOMY_POSTURE": "full"},
     "goals and scheduled jobs run", "does not tell"),
    ({"AUTONOMY_ENABLED": "true", "AUTONOMY_POSTURE": "full"}, "scheduled jobs run", "goals"),
    ({"AUTONOMY_ENABLED": "true"}, "nothing runs, although", "goals"),
])
def test_away_says_what_really_runs(monkeypatch, env, right, wrong):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    line = next(ln for ln in pc.build_answers() if ln.startswith("When you are away"))
    assert right in line and wrong not in line


def test_money_names_the_limit_that_runs_without_asking(monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setattr("core.config_policy.spend_lane.autonomous_ceiling_usd", lambda: 25.0)
    assert "Money: up to $25 a payment without asking" in _text()


def test_level_three_without_the_local_profile_is_not_the_host(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    constants._refreeze_compute_posture_for_tests()
    assert "Commands: only in a sandbox, because" in _text()


def test_autonomous_mode_still_waits_for_an_owner_pin(monkeypatch):
    monkeypatch.setattr(pc, "effective_approval_provider", lambda u=None: "auto_notify")
    monkeypatch.setattr(pc, "asks_first_actions", lambda u=None: ["filesystem_write_file", "git_push"])
    monkeypatch.setattr(pc, "_pref_asks", lambda u=None: {"filesystem_write_file"})
    monkeypatch.setattr(pc, "_always_gated", lambda: set())
    sentence, really = pc.asks_first_answer()
    assert really == ["filesystem_write_file"]
    assert "`git_push` runs, and it tells you afterward" in sentence
    assert "change files" not in _text()


def test_only_the_owner_gets_the_terminal_tools_on_a_local_console(monkeypatch):
    import tools.controller.approval as approval
    from api.tenant_tools import default_session_tools
    monkeypatch.setattr(approval, "_CONSOLE_PROCESS", True)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    assert "coding" in default_session_tools("owner-1")
    assert "coding" not in default_session_tools("tenant-9")


def test_the_task_route_refuses_a_terminal_chat(monkeypatch):
    from fastapi import HTTPException
    import tools.controller.approval as approval
    from api.tenant_tools import refuse_terminal_chat
    refuse_terminal_chat({"creator": "cli"}, "s1")  # not a console process: the API server sends
    monkeypatch.setattr(approval, "_CONSOLE_PROCESS", True)
    refuse_terminal_chat({"creator": "api"}, "s1")
    with pytest.raises(HTTPException):
        refuse_terminal_chat({"creator": "cli"}, "s1")
