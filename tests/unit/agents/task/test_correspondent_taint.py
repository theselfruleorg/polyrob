"""B5 (2026-07-13 correspondent review): taint is lock-guarded and source-tracked.

The taint flag was a bare bool written from two racing paths (owner turn clears,
correspondent inject sets) with no record of WHO tainted the session. Now:
- `_set_correspondent_taint(surface, address)` / `_clear_correspondent_taint()`
  mutate under a lock;
- `_correspondent_taint_sources` records each (surface, normalized-address) so the
  scoped reply exemption (D1) can allow replying to exactly the tainting party;
- the legacy `_correspondent_tainted` bool keeps working (gate back-compat).
"""
import asyncio

import pytest

from agents.task.session.hitl_ingress import HITLIngressMixin
from modules.llm.messages import MessageOrigin  # noqa: F401 (import parity)


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _Agent:
    def __init__(self):
        self.message_manager = _MM()


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Orch(HITLIngressMixin):
    def __init__(self, agents):
        self.agents = agents
        self.logger = _Logger()


def test_set_and_clear_taint_tracks_sources():
    orch = _Orch({"a": _Agent()})
    orch._set_correspondent_taint("email", "John@Acme.com ")
    assert orch._correspondent_tainted is True
    assert ("email", "john@acme.com") in orch._correspondent_taint_sources
    orch._set_correspondent_taint("x", "12345")
    assert len(orch._correspondent_taint_sources) == 2
    orch._clear_correspondent_taint()
    assert orch._correspondent_tainted is False
    assert orch._correspondent_taint_sources == set()


def test_inject_records_source_surface_and_address():
    orch = _Orch({"a": _Agent()})
    ok = orch.inject_correspondent_message(
        "reply body", "john@acme.com", surface="email", address="john@acme.com")
    assert ok is True
    assert orch._correspondent_tainted is True
    assert ("email", "john@acme.com") in orch._correspondent_taint_sources


def test_inject_without_surface_falls_back_to_source_label():
    orch = _Orch({"a": _Agent()})
    orch.inject_correspondent_message("reply body", "john@acme.com")
    assert orch._correspondent_tainted is True
    assert ("", "john@acme.com") in orch._correspondent_taint_sources


@pytest.mark.asyncio
async def test_owner_turn_clears_taint_and_sources():
    orch = _Orch({})  # no agents -> submit stores in pending queue
    orch._pending_messages = []
    orch._pending_messages_lock = asyncio.Lock()
    orch._set_correspondent_taint("email", "john@acme.com")
    await orch.submit_user_message(None, "owner speaking", kind="comment")
    assert orch._correspondent_tainted is False
    assert orch._correspondent_taint_sources == set()
