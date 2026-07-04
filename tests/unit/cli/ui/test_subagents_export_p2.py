"""P2 CLI polish: /subagents live background delegations + /export to session root.

Locks in the two handlers.py behavior changes:
- _h_subagents now surfaces the orchestrator's live async-delegation records
  (not just static config).
- _h_export defaults its output file into the session workspace (SSOT), not CWD.
"""
from __future__ import annotations

import io
import json

from cli.ui.commands.handlers import _h_subagents, _h_export
from cli.ui.commands.registry import CommandContext


class _CapRenderer:
    def __init__(self):
        self.buf = io.StringIO()

    def print_block(self, text, title="", style=""):
        self.buf.write(text + "\n")


def _ctx(**kw):
    r = _CapRenderer()
    return CommandContext(renderer=r, user_id="local", session_id="sess-abcdef123456", **kw), r


# ---- /subagents live activity ---------------------------------------------


class _Rec:
    def __init__(self, did, goal, status):
        self.delegation_id = did
        self.goal = goal
        self.status = status


class _Reg:
    def __init__(self, records):
        self._records = records

    def list(self):
        return list(self._records)


class _Orch:
    def __init__(self, records):
        self.async_delegation = _Reg(records)


def test_subagents_lists_live_background_delegations():
    orch = _Orch([_Rec("deadbeef0001", "research the pricing page", "running"),
                  _Rec("deadbeef0002", "draft the summary", "completed")])
    ctx, r = _ctx(orchestrator=orch)
    _h_subagents(ctx)
    out = r.buf.getvalue()
    assert "Background delegations (2)" in out
    assert "deadbeef" in out or "deadbee" in out
    assert "research the pricing page" in out
    assert "draft the summary" in out


def test_subagents_no_orchestrator_is_graceful():
    ctx, r = _ctx()  # orchestrator is None
    _h_subagents(ctx)
    out = r.buf.getvalue()
    assert "Delegation:" in out
    assert "No active background delegations." in out


def test_subagents_empty_registry_says_none():
    ctx, r = _ctx(orchestrator=_Orch([]))
    _h_subagents(ctx)
    assert "No active background delegations." in r.buf.getvalue()


def test_subagents_failing_registry_fails_open():
    class _BadReg:
        def list(self):
            raise RuntimeError("boom")

    class _BadOrch:
        async_delegation = _BadReg()

    ctx, r = _ctx(orchestrator=_BadOrch())
    _h_subagents(ctx)  # must not raise
    assert "Delegation:" in r.buf.getvalue()  # static info still shown


# ---- /export defaults into the session workspace --------------------------


def test_export_defaults_into_session_dir(tmp_path, monkeypatch):
    # Point pm().get_session_root at a temp dir and assert the file lands there,
    # not in CWD.
    session_dir = tmp_path / "sess" / "workspace"

    class _PM:
        def get_session_root(self, sid, uid):
            return session_dir

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())

    convo = type("C", (), {"turns": []})()
    ctx, r = _ctx(conversation=convo)
    ctx.args = ["json"]
    _h_export(ctx)

    written = list(session_dir.glob("*_export.json"))
    assert len(written) == 1, f"expected one export in session dir, got {written}"
    data = json.loads(written[0].read_text())
    assert data["session_id"] == "sess-abcdef123456"
    # The emitted confirmation names the absolute session-dir path, not a bare CWD file.
    assert str(session_dir) in r.buf.getvalue()
