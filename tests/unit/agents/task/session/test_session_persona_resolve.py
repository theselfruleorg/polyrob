"""The persona reaches EVERY session, not only the /v1 front door and the CLI.

Chat surfaces, cron and goal runs and A2A tasks all build their orchestrator
through ``create_session``, which sets no ``_persona_block``. Before this seam
the character never reached the model on those paths (proved on a live install:
an owner chat and a cron run both had a bare ``<identity>``). ``create_agent``
now resolves it through the ONE resolver when no front door set it.
"""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest

import agents.task.agent  # noqa: F401 — import order: the package first (circular import)
from agents.task.session.execution import SessionExecutionMixin


class _Stop(Exception):
    pass


class _Orch(SessionExecutionMixin):
    def __init__(self, persona=None, set_persona=False):
        self.session_id = "s1"
        self.user_id = "u1"
        self.container = None
        self.logger = logging.getLogger("test")
        if set_persona:
            self._persona_block = persona


def _capture(monkeypatch):
    from agents.task.agent.service import Agent
    captured = {}

    def fake_from_params(**kw):
        captured.update(kw)
        raise _Stop()

    monkeypatch.setattr(Agent, "from_params", staticmethod(fake_from_params))
    return captured


def _run(orch):
    with pytest.raises(Exception):
        asyncio.run(orch.create_agent(task="t", llm=MagicMock()))


def _resolver(monkeypatch, text, calls=None):
    import agents.personality.persona_resolver as pr

    async def fake(container=None):
        if calls is not None:
            calls.append(1)
        return text

    monkeypatch.setattr(pr, "resolve_persona", fake)


def test_unset_persona_is_resolved_and_forwarded(monkeypatch):
    captured = _capture(monkeypatch)
    _resolver(monkeypatch, "You are Nova.")
    orch = _Orch()
    _run(orch)
    assert captured["persona_block"] == "You are Nova."
    assert orch._persona_block == "You are Nova."


def test_resolved_once_per_orchestrator(monkeypatch):
    _capture(monkeypatch)
    calls = []
    _resolver(monkeypatch, "You are Nova.", calls)
    orch = _Orch()
    _run(orch)
    _run(orch)  # a sub-agent on the same orchestrator
    assert len(calls) == 1


def test_a_front_door_persona_wins(monkeypatch):
    captured = _capture(monkeypatch)
    calls = []
    _resolver(monkeypatch, "resolver text", calls)
    _run(_Orch(persona="front door text", set_persona=True))
    assert captured["persona_block"] == "front door text"
    assert calls == []


def test_gate_off_keeps_the_prompt_byte_identical(monkeypatch):
    captured = _capture(monkeypatch)
    _resolver(monkeypatch, "")
    _run(_Orch())
    assert "persona_block" not in captured


def test_a_broken_resolver_never_stops_the_run(monkeypatch):
    captured = _capture(monkeypatch)
    import agents.personality.persona_resolver as pr

    async def boom(container=None):
        raise RuntimeError("bad character")

    monkeypatch.setattr(pr, "resolve_persona", boom)
    _run(_Orch())
    assert "persona_block" not in captured


def test_real_resolver_reaches_the_prompt_under_local_mode(monkeypatch, tmp_path):
    """End to end through the real resolver: a data-home character renders."""
    import json

    chars = tmp_path / "characters"
    chars.mkdir()
    (chars / "nova.character.json").write_text(json.dumps({
        "name": "Nova", "bio": "A dry test agent.",
        "postExamples": ["shipped it, receipts below"],
    }))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "nova")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    captured = _capture(monkeypatch)
    _run(_Orch())
    block = captured["persona_block"]
    assert "You are Nova." in block
    assert "A dry test agent." in block
    assert "shipped it, receipts below" in block
