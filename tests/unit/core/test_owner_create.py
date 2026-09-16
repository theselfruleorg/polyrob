"""043 A5 — the ONE owner-create SSOT (``core.owner_create``).

The owner seat (the console, ``polyrob goals create``, Telegram ``/trade``)
creates work with the OWNER's grant: named tools are written VERBATIM, never run
through the agent's self-grant allowlist. This proves the policy in the pure
helper (with an injected board/service, per the layering rule) AND end-to-end
against a real ``GoalBoard`` / ``CronService``.

⚠️ Reach, never policy: naming ``defi_trade`` here writes a string into a DB row;
nothing runs a money verb. A dispatcher runs the row later under the same caps
and owner queue as any other.
"""
import pytest

from core.owner_create import create_cron, create_goal


class _FakeBoard:
    """Records the kwargs ``create`` is called with — the injection seam."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("G", (), {"id": "g1", "status": kwargs.get("status")})()


class _FakeService:
    def __init__(self):
        self.calls = []

    def schedule(self, **kwargs):
        self.calls.append(kwargs)
        return type("J", (), {"id": "j1"})()


# --- create_goal: the owner grant is UNFILTERED ----------------------------- #

def test_create_goal_writes_tools_verbatim_not_the_self_grant_allowlist():
    board = _FakeBoard()
    create_goal(board, user_id="u1", title="ship it",
                tools=["defi_trade", "filesystem"])
    payload = board.calls[0]["payload"]
    # A money tool the agent's own goal_create would STRIP survives here: the
    # owner IS the operator grant.
    assert payload["tools"] == ["defi_trade", "filesystem"]


def test_create_goal_strips_blank_tool_ids_but_does_not_filter():
    board = _FakeBoard()
    create_goal(board, user_id="u1", title="t", tools=["  defi_trade ", "", "  "])
    assert board.calls[0]["payload"]["tools"] == ["defi_trade"]


def test_create_goal_no_tools_writes_no_tools_key():
    board = _FakeBoard()
    create_goal(board, user_id="u1", title="t")
    # payload is None (no tools, no acceptance) so dispatch's wide default applies.
    assert board.calls[0]["payload"] is None


def test_create_goal_carries_acceptance_and_tenant_and_priority():
    board = _FakeBoard()
    create_goal(board, user_id="alice", title="t", priority=9,
                acceptance="the file exists at out.txt")
    call = board.calls[0]
    assert call["user_id"] == "alice"
    assert call["priority"] == 9
    assert call["payload"]["acceptance"] == "the file exists at out.txt"


def test_create_goal_rejects_an_empty_title_before_any_write():
    board = _FakeBoard()
    with pytest.raises(ValueError):
        create_goal(board, user_id="u1", title="   ")
    assert board.calls == []  # refused before the row


def test_create_goal_rejects_an_anonymous_tenant():
    board = _FakeBoard()
    with pytest.raises(ValueError):
        create_goal(board, user_id="", title="t")
    assert board.calls == []


def test_create_goal_reraises_the_stores_duplicate_error():
    class _Dup(ValueError):
        pass

    class _Board:
        def create(self, **kwargs):
            raise _Dup("near-duplicate")

    with pytest.raises(_Dup):
        create_goal(_Board(), user_id="u1", title="t")


# --- create_cron: same grant, plus the A29 via passthrough ------------------ #

def test_create_cron_passes_via_and_tools_verbatim():
    svc = _FakeService()
    create_cron(svc, task="summarise the week", schedule_spec="30m",
                user_id="u1", tools=["defi_trade"], via="webview")
    call = svc.calls[0]
    assert call["via"] == "webview"
    assert call["payload"]["tools"] == ["defi_trade"]
    assert call["task"] == "summarise the week"
    assert call["schedule_spec"] == "30m"


def test_create_cron_carries_delivery_and_wake_flags():
    svc = _FakeService()
    create_cron(svc, task="daily digest run", schedule_spec="1d", user_id="u1",
                deliver="telegram", deliver_target="123", wake_agent=False)
    payload = svc.calls[0]["payload"]
    assert payload["deliver"] == "telegram"
    assert payload["deliver_target"] == "123"
    assert payload["wake_agent"] is False


def test_create_cron_rejects_empty_task_and_empty_schedule():
    svc = _FakeService()
    with pytest.raises(ValueError):
        create_cron(svc, task="   ", schedule_spec="30m", user_id="u1")
    with pytest.raises(ValueError):
        create_cron(svc, task="do a thing", schedule_spec="  ", user_id="u1")
    assert svc.calls == []


def test_create_cron_reraises_schedule_error():
    from cron.schedule import ScheduleError

    class _Svc:
        def schedule(self, **kwargs):
            raise ScheduleError("bad spec")

    with pytest.raises(ScheduleError):
        create_cron(_Svc(), task="do a thing", schedule_spec="not-a-schedule",
                    user_id="u1")


# --- end-to-end against the real stores ------------------------------------- #

def test_create_goal_against_a_real_board_lands_owner_tenant_and_tools(tmp_path):
    from agents.task.goals.board import GoalBoard

    board = GoalBoard(str(tmp_path / "goals.db"))
    goal = create_goal(board, user_id="u1", title="ship the widget",
                       tools=["defi_trade", "filesystem"])
    stored = board.get(goal.id, user_id="u1")
    assert stored is not None
    assert stored.user_id == "u1"
    assert stored.payload.get("tools") == ["defi_trade", "filesystem"]


def test_create_cron_against_a_real_service_rejects_a_bad_schedule(tmp_path):
    from cron.jobs import CronJobStore
    from cron.schedule import ScheduleError
    from cron.service import CronService

    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    with pytest.raises(ScheduleError):
        create_cron(svc, task="do a real thing", schedule_spec="not-a-schedule",
                    user_id="u1")


def test_create_cron_against_a_real_service_schedules_and_audits(tmp_path):
    from cron.jobs import CronJobStore
    from cron.service import CronService

    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    job = create_cron(svc, task="summarise the week", schedule_spec="30m",
                      user_id="u1", via="webview")
    assert job.id
    assert job.id in {j.id for j in svc.list_jobs(user_id="u1")}
