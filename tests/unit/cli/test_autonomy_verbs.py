"""030 WS-E4 — `polyrob autonomy` intent verbs (the 026-P3 mode dial).

`status` must render every posture axis (the ONE card from
core.config_policy.posture_card), `on`/`off` must write the RIGHT flags through
core.config_service.set_value and echo its honesty notes (clamp echo,
"restart"), and `halt`/`resume` must be live aliases of the owner kill-switch
primitive.

The Telegram `/mode` verb (read-only v1, same card) is tested here too —
tests/unit/cli/ is this workstream's test home; the fixtures mirror
tests/unit/surfaces/telegram/test_owner_control_plane.py, whose three-list
pin tests (routable / menu / owner-admin) cover `/mode` membership once the
verb joins `_OWNER_ADMIN_COMMANDS`.
"""
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Home + CWD + data home at tmp; env-ladder loading neutralized."""
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data_home"))
    # the group callback's ensure_env_loaded is memoized process-wide; short-
    # circuit it so no test depends on which test triggered the first load_env.
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", True)
    return home, proj


def _runner():
    from cli.commands.autonomy import autonomy
    return CliRunner(), autonomy


# ---------------------------------------------------------------------------
# status — every axis of the ONE posture card
# ---------------------------------------------------------------------------

def test_status_renders_all_axes(isolated):
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["status"])
    assert res.exit_code == 0, res.output
    for axis in ("trust profile", "autonomy master", "capability mode",
                 "loop posture", "compute posture", "owner pause"):
        assert axis in res.output, f"axis {axis!r} missing:\n{res.output}"
    # halt state + the REPL /autonomy loop-flag list
    assert "pause:" in res.output  # 031: the pause line replaced the halt line
    for loop in ("self-wake", "goals", "curator", "cron-run-loop",
                 "background-review"):
        assert loop in res.output, f"loop flag {loop!r} missing:\n{res.output}"


def test_status_json_is_machine_readable(isolated):
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["status", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    axes = {row["axis"] for row in payload["posture_card"]}
    assert {"autonomy master", "capability mode", "loop posture",
            "compute posture"} <= axes
    assert isinstance(payload["halted"], bool)
    assert "goals" in payload["loops"]


# ---------------------------------------------------------------------------
# on / off — the one write path + its honesty notes
# ---------------------------------------------------------------------------

def test_on_writes_autonomy_enabled_and_prints_restart_note(isolated):
    _home, proj = isolated
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["on"])
    assert res.exit_code == 0, res.output
    env = (proj / ".polyrob" / ".env").read_text()
    assert "AUTONOMY_ENABLED=true" in env
    assert "restart" in res.output  # env flags configure the NEXT process


def test_on_with_mode_writes_both_flags_and_echoes_clamp(isolated, monkeypatch):
    _home, proj = isolated
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)  # clamp is guaranteed
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["on", "--mode", "autonomous"])
    assert res.exit_code == 0, res.output
    env = (proj / ".polyrob" / ".env").read_text()
    assert "AUTONOMY_ENABLED=true" in env
    assert "AUTONOMY_MODE=autonomous" in env
    # the 026 P1.6 clamp echo from the ONE note builder (post_write_notes)
    assert "CLAMP" in res.output


def test_on_rejects_invalid_mode_before_writing_anything(isolated):
    _home, proj = isolated
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["on", "--mode", "autonmous"])
    assert res.exit_code != 0
    assert "supervised" in res.output  # names the valid set (flag_enums SSOT)
    assert not (proj / ".polyrob" / ".env").exists(), \
        "an invalid --mode must not half-apply the intent"


def test_off_writes_false_and_names_the_live_kill_switch(isolated):
    _home, proj = isolated
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["off"])
    assert res.exit_code == 0, res.output
    assert "AUTONOMY_ENABLED=false" in (proj / ".polyrob" / ".env").read_text()
    assert "pause" in res.output  # off ≠ now; the live pause is the switch


def test_on_never_touches_money_or_compute_flags(isolated):
    _home, proj = isolated
    runner, autonomy = _runner()
    res = runner.invoke(autonomy, ["on", "--mode", "autonomous"])
    assert res.exit_code == 0, res.output
    env = (proj / ".polyrob" / ".env").read_text()
    for forbidden in ("AGENT_COMPUTE_POSTURE", "PAYMENT_APPROVAL_MODE",
                      "WALLET_DAILY_CAP_USD"):
        assert forbidden not in env


# ---------------------------------------------------------------------------
# halt / resume — live aliases of the owner kill-switch
# ---------------------------------------------------------------------------

def test_halt_then_resume_round_trip(isolated, tmp_path, monkeypatch):
    data_home = tmp_path / "halt_home"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    runner, autonomy = _runner()

    res = runner.invoke(autonomy, ["halt"])
    assert res.exit_code == 0, res.output
    assert "HALTED" in res.output or "Paused everything" in res.output
    from core.autonomy_control import PAUSE_FILENAME
    assert (data_home / PAUSE_FILENAME).exists()  # 031: the one record
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.autonomy_halted() is True

    res = runner.invoke(autonomy, ["resume"])
    assert res.exit_code == 0, res.output
    assert "RESUMED" in res.output
    assert not (data_home / PAUSE_FILENAME).exists()
    assert AutonomyConfig.autonomy_halted() is False


def test_resume_is_honest_about_an_env_halt(isolated, tmp_path, monkeypatch):
    data_home = tmp_path / "halt_home"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    runner, autonomy = _runner()
    runner.invoke(autonomy, ["halt"])
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    res = runner.invoke(autonomy, ["resume"])
    assert res.exit_code == 0, res.output
    assert "AUTONOMY_HALT" in res.output  # the env halt survives; say so


# ---------------------------------------------------------------------------
# registration — the verb group is reachable through the lazy registry
# ---------------------------------------------------------------------------

def test_autonomy_is_lazily_registered():
    from cli.polyrob import _LAZY_SUBCOMMANDS
    assert _LAZY_SUBCOMMANDS.get("autonomy") == "cli.commands.autonomy:autonomy"


# ---------------------------------------------------------------------------
# Telegram /mode — read-only v1: JUST the posture card + how to change it
# (fixtures mirror tests/unit/surfaces/telegram/test_owner_control_plane.py)
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(command, text, user_id="alice"):
    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.inbound import InboundResult
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user_id, source=source))
    decision = RouteDecision(kind=RouteKind.COMMAND, session_key="telegram:1",
                             session_id=None, command=command)
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def tg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    return tmp_path


def test_mode_is_in_all_three_command_lists():
    """The three-list landmine: dispatcher routing + owner gating + help SSOT."""
    from core.surfaces.dispatcher import _COMMANDS
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS, help_commands
    assert "/mode" in _COMMANDS
    assert "/mode" in _OWNER_ADMIN_COMMANDS
    assert "mode" in {name for name, _ in help_commands()}


@pytest.mark.asyncio
async def test_mode_routes_as_command_end_to_end(tg_env):
    from core.surfaces.dispatcher import RouteKind, route_inbound
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    source = SessionSource(surface_id="telegram", chat_id="1", chat_type="dm")
    inbound = InboundMessage(text="/mode",
                             identity=Identity(user_id="alice", source=source))
    decision = await route_inbound(None, inbound)
    assert decision.kind is RouteKind.COMMAND
    assert decision.command == "/mode"


@pytest.mark.asyncio
async def test_mode_renders_card_and_change_hint(tg_env):
    from surfaces.telegram.harness import act_on_inbound
    out = await act_on_inbound(_Agent(str(tg_env)), _cmd("/mode", "/mode"))
    for axis in ("autonomy master", "capability mode", "loop posture",
                 "compute posture"):
        assert axis in out, f"axis {axis!r} missing:\n{out}"
    # how to change: mode/posture are env flags, not chat prefs
    assert "polyrob autonomy on|off" in out
    assert "prefs-only" in out


@pytest.mark.asyncio
async def test_mode_refused_for_non_owner(tg_env):
    from surfaces.telegram.harness import act_on_inbound
    out = await act_on_inbound(_Agent(str(tg_env)),
                               _cmd("/mode", "/mode", user_id="stranger"))
    assert "Owner only" in out
