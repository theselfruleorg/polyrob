"""One shared activity-evidence layer (audit T8, 2026-07-16).

cron/digest.py (daily digest) and core/recap.py (polyrob recap) previously
reimplemented the SAME ledger/episode reads — the owner-facing numbers could
silently diverge. Both now delegate to core/activity_evidence.py.
"""
import inspect

import core.activity_evidence as ae


def test_ledger_rollup_fails_open(monkeypatch):
    async def _boom(user_id, days=1):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", _boom)
    assert ae.ledger_rollup("u1", 1) == {}


def test_recent_episodes_normalizes(monkeypatch):
    class _Row:
        kind = "goal"
        outcome = "done"
        spend_usd = 0.5
        task = "t"
        ts = 123

    async def _rows(**kw):
        return [_Row()]

    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", _rows)
    rows = ae.recent_episodes("u1", None)
    assert rows == [{"kind": "goal", "outcome": "done", "spend_usd": 0.5,
                     "task": "t", "ts": 123}]


def test_recent_episodes_fails_open(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("memory down")

    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", _boom)
    assert ae.recent_episodes("u1", None) == []


def test_digest_and_recap_delegate():
    """The seams stay (tests monkeypatch cron.digest._ledger etc.) but their
    bodies must route through the shared layer."""
    import cron.digest as digest
    import core.recap as recap

    for fn in (digest._ledger, digest._episodes, recap._ledger, recap._episodes):
        assert "activity_evidence" in inspect.getsource(fn), fn.__module__
