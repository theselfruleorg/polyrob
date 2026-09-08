"""Owner ↔ dev-loop rail (/dev, proposal 027 WS-1): free-text forwarding to the
on-host Claude dev loop via scripts/dev_inject.sh.

Owner-gated by principal (network surface → NO local bypass); never reaches the
agent; graceful when the inject script is absent (public installs don't ship
scripts/).
"""
import stat

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram import dev_rail
from surfaces.telegram.harness import _HELP_BODY, act_on_inbound
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
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(text, user="gleb"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command="/dev"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from core.instance import DEFAULT_INSTANCE_ID
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def _fake_script(tmp_path, body):
    p = tmp_path / "dev_inject.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_dev_is_a_routable_command():
    assert "/dev" in _COMMANDS


def test_dev_in_help():
    assert "/dev" in _HELP_BODY


def test_free_text_preserved():
    # The rail must receive the raw remainder, not a re-joined token list.
    assert (dev_rail.strip_dev_prefix("/dev fix   the — spacing\nsecond line")
            == "fix   the — spacing\nsecond line")
    assert dev_rail.strip_dev_prefix("/dev") == ""


@pytest.mark.asyncio
async def test_dev_refused_for_non_owner(env, monkeypatch):
    called = {}

    async def _boom(text):
        called["text"] = text
        return "should never run"

    monkeypatch.setattr(dev_rail, "perform_dev_command", _boom)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev hi", user="u_stranger"))
    assert "owner" in out.lower()
    assert not called


@pytest.mark.asyncio
async def test_dev_unavailable_without_script(env, monkeypatch, tmp_path):
    monkeypatch.setenv("DEV_RAIL_SCRIPT", str(tmp_path / "missing.sh"))
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev hello loop"))
    assert "not available" in out.lower()


@pytest.mark.asyncio
async def test_dev_live_injection_passes_raw_text(env, monkeypatch, tmp_path):
    script = _fake_script(tmp_path, 'cat > "$0.msg"\nexit 0\n')
    monkeypatch.setenv("DEV_RAIL_SCRIPT", script)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev fix the thing please"))
    assert "dev loop" in out.lower()
    assert (tmp_path / "dev_inject.sh.msg").read_text() == "fix the thing please"


@pytest.mark.asyncio
async def test_dev_queued_when_loop_down(env, monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat >/dev/null\nexit 3\n")
    monkeypatch.setenv("DEV_RAIL_SCRIPT", script)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev hello"))
    assert "inbox" in out.lower()


@pytest.mark.asyncio
async def test_dev_error_surfaces_stderr(env, monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat >/dev/null\necho kaput >&2\nexit 4\n")
    monkeypatch.setenv("DEV_RAIL_SCRIPT", script)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev hello"))
    assert "kaput" in out


@pytest.mark.asyncio
async def test_bare_dev_reports_status(env, monkeypatch, tmp_path):
    script = _fake_script(
        tmp_path,
        'if [ "$1" = "--status" ]; then echo "dev loop: up"; exit 0; fi\nexit 4\n')
    monkeypatch.setenv("DEV_RAIL_SCRIPT", script)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/dev"))
    assert "dev loop: up" in out
