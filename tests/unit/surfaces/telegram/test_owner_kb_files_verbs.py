"""Telegram owner verbs /kb + /files (QW-4, 2026-07-19, proposal 021): the
phone-first owner's READ path into the knowledge base and the run artifacts.
"Ingested into KB" was write-only theatre from the phone; /files turns the
episode artifact registry into something the owner can actually see.

Mirrors test_owner_status_verbs.py — owner-gated by principal; primitives are
the SAME ones the CLI uses (modules.memory.registry.kb_search /
memory_recall_episodes).
"""
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
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


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


def test_kb_files_are_routable_owner_commands():
    for verb in ("/kb", "/files"):
        assert verb in _COMMANDS
        assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.parametrize("verb", ["/kb", "/files"])
@pytest.mark.asyncio
async def test_verb_refused_for_non_owner(env, verb):
    out = await act_on_inbound(_Agent(str(env)), _cmd(verb, verb, user="u_stranger"))
    assert "owner" in out.lower()


@pytest.mark.asyncio
async def test_kb_requires_query(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/kb", "/kb"))
    assert "usage" in out.lower()


@pytest.mark.asyncio
async def test_kb_returns_search_results(env, monkeypatch):
    monkeypatch.setenv("KB_ENABLED", "true")
    import modules.memory.registry as reg

    async def _fake_search(query, *, user_id=None, collection="default", limit=8):
        assert query == "x402 services"
        assert user_id == "gleb"
        return "1. x402-recon.md — Quicknode, Vybe, Venice"

    monkeypatch.setattr(reg, "kb_search", _fake_search)
    out = await act_on_inbound(_Agent(str(env)),
                               _cmd("/kb", "/kb x402 services"))
    assert "Quicknode" in out


@pytest.mark.asyncio
async def test_kb_honest_when_disabled(env, monkeypatch):
    monkeypatch.setenv("KB_ENABLED", "false")
    out = await act_on_inbound(_Agent(str(env)), _cmd("/kb", "/kb anything"))
    assert "disabled" in out.lower() or "not enabled" in out.lower()


@pytest.mark.asyncio
async def test_kb_honest_when_no_results(env, monkeypatch):
    monkeypatch.setenv("KB_ENABLED", "true")
    import modules.memory.registry as reg

    async def _fake_search(query, **kw):
        return ""

    monkeypatch.setattr(reg, "kb_search", _fake_search)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/kb", "/kb nothing"))
    assert "no results" in out.lower()


@pytest.mark.asyncio
async def test_files_lists_recent_artifacts(env, monkeypatch):
    import modules.memory.registry as reg

    async def _fake_episodes(**kw):
        assert kw.get("user_id") == "gleb"
        return [
            {"session_id": "s-new", "ts": 200, "artifacts": [
                {"path": "x402-recon.md", "bytes": 3661, "mtime": 200},
                {"kind": "message", "detail": "message[open] -> telegram:owner OK"},
            ]},
            {"session_id": "s-old", "ts": 100, "artifacts":
                '[{"path": "old-report.md", "bytes": 100, "mtime": 100}]'},
        ]

    monkeypatch.setattr(reg, "memory_recall_episodes", _fake_episodes)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/files", "/files"))
    assert "x402-recon.md" in out
    assert "old-report.md" in out  # JSON-string artifacts parsed too


@pytest.mark.asyncio
async def test_files_honest_when_empty(env, monkeypatch):
    import modules.memory.registry as reg

    async def _fake_episodes(**kw):
        return []

    monkeypatch.setattr(reg, "memory_recall_episodes", _fake_episodes)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/files", "/files"))
    assert "no " in out.lower()
