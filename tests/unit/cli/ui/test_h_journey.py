"""/journey — pure renderer unions episodes/events/authored-skills/ledger."""
from types import SimpleNamespace

from cli.ui.commands import h_journey


def test_render_journey_sections(monkeypatch):
    monkeypatch.setattr(h_journey, "_episodes", lambda uid, since: [
        {"kind": "goal", "outcome": "done", "spend_usd": 0.2, "task": "ship X"}])
    monkeypatch.setattr(h_journey, "_events", lambda uid, since: [
        {"kind": "self_modification", "attrs": {"kind": "skill", "skill_id": "s1"}}])
    monkeypatch.setattr(h_journey, "_authored", lambda uid, data_dir=None: [
        {"skill_id": "s1", "load_count": 3, "created_by": "agent"}])
    monkeypatch.setattr(h_journey, "_ledger", lambda uid, days: {
        "earned_usd": 1.5, "settled_payments": 1, "total_spend_usd": 0.2, "net_usd": 1.3})
    out = h_journey.render_journey(user_id="u1", since_label="7d")
    assert "ship X" in out
    assert "Earned: $1.50" in out and "net $1.30" in out
    assert "s1" in out and "used 3x" in out
    assert "Changed:" in out and "skill s1" in out


def test_render_journey_all_empty_is_stable(monkeypatch):
    # every source fails open to empty — the renderer still produces all sections.
    monkeypatch.setattr(h_journey, "_episodes", lambda *a, **k: [])
    monkeypatch.setattr(h_journey, "_events", lambda *a, **k: [])
    monkeypatch.setattr(h_journey, "_authored", lambda *a, **k: [])
    monkeypatch.setattr(h_journey, "_ledger", lambda *a, **k: {})
    out = h_journey.render_journey(user_id="u1", since_label="24h")
    assert "Did:" in out and "no episodes" in out
    assert "Earned: $0.00" in out
    assert "no authored skills" in out
    assert "no self-modifications" in out


def test_window_seconds_parse():
    assert h_journey._window_seconds("24h") == 24 * 3600
    assert h_journey._window_seconds("7d") == 7 * 86400
    assert h_journey._window_seconds("") is None
    assert h_journey._window_seconds("junk") is None


def test_h_journey_handler_emits(monkeypatch):
    monkeypatch.setattr(h_journey, "render_journey",
                        lambda **kw: f"RENDERED:{kw['user_id']}:{kw['since_label']}")
    emitted = {}
    ctx = SimpleNamespace(
        args=["24h"], user_id="u1", container=None,
        emit=lambda text, title=None: emitted.update({"text": text, "title": title}))
    h_journey.h_journey(ctx)
    assert emitted["text"] == "RENDERED:u1:24h"
    assert emitted["title"] == "journey"


def test_ledger_seam_works_inside_running_loop(monkeypatch):
    # Regression: the REPL dispatches this sync handler INSIDE a running event loop,
    # where a bare asyncio.run() raises -> fail-open empties the money section. The
    # loop-safe bridge must return the real ledger even under a running loop.
    import asyncio

    async def _fake_build(user_id, *, days=7, db=None):
        return {"earned_usd": 4.0, "total_spend_usd": 1.0, "net_usd": 3.0}

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", _fake_build)

    async def _driver():
        # calling the SYNC seam from within a running loop (mirrors the REPL)
        return h_journey._ledger("u1", 7)

    result = asyncio.run(_driver())
    assert result.get("earned_usd") == 4.0  # NOT {} from a swallowed RuntimeError
