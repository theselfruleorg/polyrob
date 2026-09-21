"""C5 / D78 (2026-09-21) — ``/approve`` DECIDES; ``/gates`` manages the gates.

``/approve`` meant two different things on two seats: the decider on Telegram
and in the console, the approval-GATE manager in this REPL. So the REPL's own
Inbox printed "approve it with /approve <id>" and the seat answered ``unknown
/approve subcommand``. One name, one meaning — with the old grammar kept as a
DEPRECATED ALIAS so an owner's muscle memory is answered, not refused.
"""
import io

import pytest

from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def _ctx(args=None, user_id="local"):
    buf = io.StringIO()
    state = SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf),
                         state=state, user_id=user_id, args=list(args or []))
    return ctx, buf


def _fake_pending(monkeypatch, items, unavailable=None):
    from tools.controller import approval_queue

    class _Set:
        def __init__(self):
            self.items = list(items)
            self.unavailable = list(unavailable or [])

        def degraded_line(self):
            if not self.unavailable:
                return ""
            return "⚠ I could not read " + ", ".join(self.unavailable)

    monkeypatch.setattr(approval_queue, "all_pending", lambda **kw: _Set())
    return _Set


# ---------------------------------------------------------------------------
# /approve = the decider
# ---------------------------------------------------------------------------


def test_bare_approve_lists_the_union(monkeypatch):
    from cli.ui.commands.h_gates import h_approve
    _fake_pending(monkeypatch, [
        {"id": "tap-abc", "kind": "tool_approval", "preview": "spend $4"},
        {"id": "skill1", "kind": "skill", "preview": "a new skill"},
    ])
    ctx, buf = _ctx([])
    h_approve(ctx)
    out = buf.getvalue()
    assert "2 waiting on you" in out
    assert "tap-abc" in out and "skill1" in out
    assert "/approve <id>" in out
    assert "/gates" in out          # the gate manager is still discoverable


def test_approve_with_an_id_decides_that_item(monkeypatch):
    from cli.ui.commands import h_gates
    _fake_pending(monkeypatch, [{"id": "tap-abc", "kind": "tool_approval",
                                 "preview": "spend $4"}])
    seen = {}

    def _decide(kind, item_id, *, approve, **kw):
        seen.update(kind=kind, item_id=item_id, approve=approve)
        return True, "Approved."

    monkeypatch.setattr("tools.controller.approval_queue.decide_pending", _decide)
    ctx, buf = _ctx(["tap-abc"])
    h_gates.h_approve(ctx)
    assert seen == {"kind": "tool_approval", "item_id": "tap-abc", "approve": True}
    assert "Approved." in buf.getvalue()


def test_approve_unknown_id_is_honest(monkeypatch):
    from cli.ui.commands.h_gates import h_approve
    _fake_pending(monkeypatch, [])
    ctx, buf = _ctx(["nope"])
    h_approve(ctx)
    assert "No pending item 'nope'" in buf.getvalue()


def test_approve_names_what_it_could_not_read(monkeypatch):
    """A partial queue is reported as partial — never as "nothing found"."""
    from cli.ui.commands.h_gates import h_approve
    _fake_pending(monkeypatch, [], unavailable=["the approval queue"])
    ctx, buf = _ctx(["nope"])
    h_approve(ctx)
    assert "could not read the approval queue" in buf.getvalue()


def test_approve_all_uses_the_one_all_decider(monkeypatch):
    from cli.ui.commands.h_gates import h_approve
    called = {}

    def _all(*, approve, **kw):
        called["approve"] = approve
        return 2, 0, ["a", "b"]

    monkeypatch.setattr("tools.controller.approval_queue.decide_all_pending", _all)
    ctx, buf = _ctx(["all"])
    h_approve(ctx)
    assert called == {"approve": True}
    assert "2 approved, 0 failed" in buf.getvalue()


# ---------------------------------------------------------------------------
# /gates = the gate manager, and the deprecated alias
# ---------------------------------------------------------------------------


def test_gates_lists_the_gate_set():
    from cli.ui.commands.h_gates import h_gates
    ctx, buf = _ctx([])
    h_gates(ctx)
    out = buf.getvalue()
    assert "approval gate" in out.lower()
    assert "provider:" in out


def test_approve_list_is_a_deprecated_alias_that_still_works():
    """An owner with the old grammar gets the ANSWER and the new name."""
    from cli.ui.commands.h_gates import h_approve
    ctx, buf = _ctx(["list"])
    h_approve(ctx)
    out = buf.getvalue()
    assert "approval gate" in out.lower()          # it still ran
    assert "gate management moved to `/gates`" in out


def test_both_verbs_are_registered():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("approve") is not None
    assert reg.lookup("gates") is not None
    assert reg.lookup("approve").group == "needs you"
