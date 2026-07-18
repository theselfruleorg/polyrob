"""Telegram owner-admin verbs (owner-UX P4 T2): /status /recap /goals /prefs —
the phone-only headless owner's read-only situational-awareness surface.

Mirrors test_owner_admin_commands.py / test_owner_allow_verb.py — owner-gated by
principal (network surface, no local bypass); a non-owner gets the existing
🔒 refusal and the underlying primitives are never touched.
"""
import os
import time

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS, act_on_inbound
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    """Bare task_agent fake — no live orchestrator (mirrors the other owner-admin
    test files' fake); /status degrades to 'no active session' against it."""

    def __init__(self, data_dir):
        self.container = _Container(data_dir)


class _FakeOrchestrator:
    def __init__(self, agents=None):
        self.agents = agents or {}


class _FakeMessageManager:
    def __init__(self, pct):
        self._pct = pct

    def get_context_usage_percent(self):
        return self._pct


class _FakeInnerAgent:
    def __init__(self, model_name, ctx_pct):
        self.model_name = model_name
        self.message_manager = _FakeMessageManager(ctx_pct)


class _ResidentAgent(_Agent):
    """A task_agent fake with a RESIDENT orchestrator for one session — exercises
    the /status running/idle + model/ctx% branch."""

    def __init__(self, data_dir, session_id, *, busy=False, model="grok-4.3", ctx_pct=42.0):
        super().__init__(data_dir)
        self._session_id = session_id
        self._busy = busy
        self._orch = _FakeOrchestrator({"main": _FakeInnerAgent(model, ctx_pct)})

    def get_orchestrator(self, session_id):
        return self._orch if session_id == self._session_id else None

    def _session_has_pending_input(self, session_id):
        return self._busy


def _cmd(command, text, user="gleb", session_id=None):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command,
        session_id=session_id))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


# --- routing --------------------------------------------------------------

def test_new_verbs_are_routable_commands():
    for verb in ("/status", "/recap", "/goals", "/prefs"):
        assert verb in _COMMANDS
        assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.asyncio
async def test_dispatcher_routes_status_as_command(tmp_path):
    """WhatsApp/email inherit this via act_on_inbound / route_inbound — one
    routing-table test that a new verb actually classifies as COMMAND."""
    from core.surfaces.dispatcher import route_inbound
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

    class _FakeContainer:
        def get_service(self, name):
            return None

    msg = InboundMessage(text="/status",
                         identity=Identity(user_id="gleb",
                                          source=SessionSource("telegram", "555", "dm")))
    decision = await route_inbound(_FakeContainer(), msg)
    assert decision.kind == RouteKind.COMMAND
    assert decision.command == "/status"


# --- owner gating (all four verbs) -----------------------------------------

@pytest.mark.parametrize("verb", ["/status", "/recap", "/goals", "/prefs"])
@pytest.mark.asyncio
async def test_verb_refused_for_non_owner(env, verb):
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd(verb, verb, user="u_stranger"))
    assert "owner" in out.lower()


# --- /status ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_no_active_session(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/status", "/status"))
    assert "no active session" in out.lower()
    assert "goals:" in out.lower()


@pytest.mark.asyncio
async def test_status_bound_and_running_session(env):
    sid = "sess_abc123"
    agent = _ResidentAgent(str(env), sid, busy=True)
    out = await act_on_inbound(agent, _cmd("/status", "/status", session_id=sid))
    assert "running" in out.lower()
    assert "grok-4.3" in out
    assert "ctx=42" in out


@pytest.mark.asyncio
async def test_status_bound_and_idle_session(env):
    sid = "sess_def456"
    agent = _ResidentAgent(str(env), sid, busy=False)
    out = await act_on_inbound(agent, _cmd("/status", "/status", session_id=sid))
    assert "idle" in out.lower()


@pytest.mark.asyncio
async def test_status_reflects_goal_counts(env):
    from agents.task.goals.board import STATUS_RUNNING, GoalBoard
    board = GoalBoard(os.path.join(str(env), "goals.db"))
    board.create(user_id="gleb", title="Draft the quarterly report")
    board.create(user_id="gleb", title="Migrate the billing database",
                 status=STATUS_RUNNING)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/status", "/status"))
    assert "1 open" in out
    assert "1 running" in out


@pytest.mark.asyncio
async def test_status_renders_ledger_lines_and_requests_balances(env, monkeypatch):
    """The two /status ledger lines (harness.py:330-331) render treasury and
    runtime as SEPARATE statements (never summed), and the lookup asks
    ``build_ledger`` for balances (``include_balances=True``) since the
    Telegram surface is a display path, not the hot non-display path that
    must skip the network balance probe."""
    calls = []

    async def fake_build_ledger(user_id, *, days=7, include_balances=False, db=None):
        calls.append({"user_id": user_id, "days": days,
                      "include_balances": include_balances})
        income_usd, spend_usd = 12.50, 3.25
        return {
            "treasury": {
                "income_usd": income_usd, "spend_usd": spend_usd,
                "net_usd": round(income_usd - spend_usd, 6),
                "pending_usd": 0.0, "pending_count": 0,
                "balance_usd": None, "available": True,
            },
            "runtime": {
                "spend_window_usd": 1.23, "spend_total_usd": 45.67,
                "calls_window": 1, "calls_total": 10,
                "provider_balance_usd": None, "available": True,
            },
        }

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger",
                        fake_build_ledger)

    out = await act_on_inbound(_Agent(str(env)), _cmd("/status", "/status"))

    assert "• Runtime cost (24h): $1.23 · $45.67 total." in out
    assert "• Treasury: net $9.25." in out
    assert len(calls) == 1
    assert calls[0]["include_balances"] is True


@pytest.mark.asyncio
async def test_status_ledger_lookup_failure_is_fail_open(env, monkeypatch):
    """A raising ``build_ledger`` must not break /status — the surrounding
    try/except (harness.py:323-333) swallows it and the rest of the status
    lines still render."""
    async def raising_build_ledger(user_id, *, days=7, include_balances=False, db=None):
        raise RuntimeError("ledger boom")

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger",
                        raising_build_ledger)

    out = await act_on_inbound(_Agent(str(env)), _cmd("/status", "/status"))

    assert "no active session" in out.lower()
    assert "goals:" in out.lower()
    assert "Runtime cost" not in out
    assert "Treasury:" not in out


# --- /recap ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recap_happy_path(env):
    from agents.task.telemetry import event_log as el
    log = el.TelemetryEventLog(str(env / "telemetry_events.db"))
    log.record("goal_run", user_id="gleb", ts=time.time(), outcome="done")

    out = await act_on_inbound(_Agent(str(env)), _cmd("/recap", "/recap"))
    assert "# Recap" in out
    assert "goal_run" in out


@pytest.mark.asyncio
async def test_recap_invalid_window(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/recap", "/recap not-a-window"))
    assert "invalid" in out.lower()
    assert "not-a-window" in out


@pytest.mark.asyncio
async def test_recap_no_activity_is_friendly(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/recap", "/recap"))
    assert "nothing to report" in out.lower()


# --- /goals ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goals_empty_board(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/goals", "/goals"))
    assert "no goals" in out.lower()


@pytest.mark.asyncio
async def test_goals_summary_with_seeded_rows(env):
    from agents.task.goals.board import STATUS_DONE, STATUS_RUNNING, GoalBoard
    board = GoalBoard(os.path.join(str(env), "goals.db"))
    board.create(user_id="gleb", title="Write the report")
    board.create(user_id="gleb", title="Deploy the release", status=STATUS_RUNNING)
    board.create(user_id="gleb", title="Old finished thing", status=STATUS_DONE)
    other = board.create(user_id="someone_else", title="Not mine")
    assert other.user_id == "someone_else"

    out = await act_on_inbound(_Agent(str(env)), _cmd("/goals", "/goals"))
    assert "3 goal(s)" in out
    assert "Write the report" in out
    assert "Deploy the release" in out
    assert "Not mine" not in out  # tenant-scoped


# --- /prefs ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prefs_shows_written_pref_with_pref_source(env):
    from core.prefs import write_preference
    ok, err = write_preference(str(env), "gleb", "style.verbosity", "terse",
                               instance_id="rob")
    assert ok, err

    out = await act_on_inbound(_Agent(str(env)), _cmd("/prefs", "/prefs"))
    assert "style.verbosity = terse (pref)" in out
    assert "tell me what to change — guarded changes arrive as /pending proposals" in out


@pytest.mark.asyncio
async def test_prefs_default_source_when_nothing_written(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/prefs", "/prefs"))
    assert "(default)" in out or "(env)" in out


# --- /help --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_help_mentions_new_verbs(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/help", "/help"))
    assert "/status" in out and "/recap" in out and "/goals" in out and "/prefs" in out


# --- /journey alias (2026-07-12 UI-surface review: one recap vocabulary) -------

def test_journey_alias_is_routable():
    assert "/journey" in _COMMANDS
    assert "/journey" in _OWNER_ADMIN_COMMANDS


@pytest.mark.asyncio
async def test_journey_alias_behaves_like_recap(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/journey", "/journey"))
    assert "nothing to report" in out.lower()
