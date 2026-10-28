"""C-3 (2026-10-14 review): <previous-runs> kept the first 300 chars of a run
summary. Run 841ac96a wrote "NOTE for the next cycle: … max_spend_usd=$10" at
char 1,267; the next run never saw it and declared $10 again. The summary now
keeps its end (head+tail clip) and the run's tool errors render first. No text
of the run is classified by its words."""
import asyncio
from types import SimpleNamespace

from agents.task.agent.core import episodic_digest as ED
from modules.memory.provider import EpisodeRecord

_LESSON = ("NOTE for the next cycle: declare max_spend_usd=$5, not $10 "
           "(SOUL's stale per-tx line).")
_DONE = ("Bought 1.2M TKN for $5.00 via defi_trade. " * 30) + _LESSON + " Done."


def _rows(monkeypatch, rows):
    async def recall(**kw):
        return rows if kw.get("thread_key") == "cron:j1" else []
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", recall)


def test_a_late_note_survives_the_head_tail_clip(monkeypatch):
    assert _DONE.index("NOTE") > 1000
    _rows(monkeypatch, [EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s1",
                                      kind="cron", outcome="done", summary=_DONE,
                                      thread_key="cron:j1", meta={})])
    out = asyncio.run(ED.render_previous_runs(user_id="u1", rail_id="cron:j1", regime="cron"))
    assert "not $10" in out
    assert len(out) <= ED.PREVIOUS_RUNS_MAX_CHARS + 600


def test_record_run_stores_the_runs_errors_and_renders_them_first(monkeypatch):
    seen = {}

    async def finalize(**kw):
        seen.update(kw)
    monkeypatch.setattr("modules.memory.episodic.finalize_episode", finalize)
    ev = SimpleNamespace(
        ledger=["1. defi_trade -> ERROR: refused: max_spend_usd $10 exceeds the per-tx cap $5",
                "2. done -> ok"],
        errors_tail=[], captured_refs=[])
    run = SimpleNamespace(done_text=_DONE, user_messages=[], artifacts=[], evidence=ev,
                          spend_usd=0.0, steps=3, skills_loaded=[])
    asyncio.run(ED.record_run(regime="cron", kind="cron", session_id="s2", user_id="u1",
                              rail_id="cron:j1", task="buy", outcome="done", run=run,
                              summary=_DONE))
    meta = seen["meta"]
    assert meta["errors"] and "exceeds the per-tx cap" in meta["errors"][0]
    assert "lessons" not in meta

    _rows(monkeypatch, [EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s2",
                                      kind="cron", outcome="done", summary=_DONE,
                                      thread_key="cron:j1", meta=meta)])
    out = asyncio.run(ED.render_previous_runs(user_id="u1", rail_id="cron:j1", regime="cron"))
    assert "error: 1. defi_trade: refused: max_spend_usd $10" in out
    assert out.index("error:") < out.index("summary:")


def test_a_legacy_refusals_row_still_renders():
    e = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s4", kind="cron",
                      outcome="done", summary="x", meta={"refusals": ["cap hit"]})
    assert "error: cap hit" in ED._render_run(e)


def test_no_error_keeps_the_summary_shape():
    e = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="s3", kind="cron",
                      outcome="done", summary="Checked the den; only joins.", meta={})
    assert ED._render_run(e).endswith("DONE: Checked the den; only joins.")
