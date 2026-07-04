"""P2a / Task 12 (ME-D4) — cron/goal tools stop minting the bespoke "system" tenant
AND stop falling back to the anonymous sentinel entirely; the goal board rejects
anonymous/sentinel tenants via the identity SSOT (findings F4/F6).

A durable cross-session goal/cron genuinely needs a real owner. A contextless call
(no execution_context.user_id) now resolves to the operator identity
(core.identity.resolve_identity(): the bound owner principal, else "local") instead
of the anon token — so it lands under a real, isolatable tenant rather than either a
shared "system" bucket or an anon bucket the board would reject outright.
"""
import pytest

from agents.task.constants import DEFAULT_USER_ID
from agents.task.goals.board import GoalBoard
from core.identity import resolve_identity
from tools.goal_tools import GoalTool
from tools.cronjob_tools import CronJobTool


class _Ctx:
    def __init__(self, uid):
        self.user_id = uid


def test_goal_tool_user_fallback_is_operator_identity_never_anon():
    assert GoalTool._user(None) == resolve_identity()
    assert GoalTool._user(None) != DEFAULT_USER_ID
    assert GoalTool._user(_Ctx(None)) == resolve_identity()
    assert GoalTool._user(_Ctx("alice")) == "alice"


def test_cron_tool_user_fallback_is_operator_identity_never_anon():
    assert CronJobTool._user(None) == resolve_identity()
    assert CronJobTool._user(None) != DEFAULT_USER_ID
    assert CronJobTool._user(_Ctx("bob")) == "bob"


def test_board_rejects_anonymous_and_sentinel_tenants(tmp_path):
    b = GoalBoard(str(tmp_path / "goals.db"))
    for bad in ("_anonymous_", "system", "", None):
        with pytest.raises(ValueError):
            b.create(user_id=bad, title="x")


def test_board_accepts_real_tenant(tmp_path):
    b = GoalBoard(str(tmp_path / "goals.db"))
    g = b.create(user_id="local", title="ok")
    assert g.user_id == "local"
