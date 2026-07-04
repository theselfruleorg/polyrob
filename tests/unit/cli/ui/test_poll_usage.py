"""Tests for SessionState.poll_usage incremental llm_usage aggregation."""

from __future__ import annotations

import json
from pathlib import Path

from cli.ui.events import AgentEnd, AgentRegistration, normalize
from cli.ui.state import SessionState


def _write_usage(usage_dir: Path, name: str, **fields) -> None:
    usage_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "component": "agent",
        "model_name": "gemini-2.5-flash",
        "provider": "gemini",
        "prompt_tokens": fields.get("prompt_tokens", 0),
        "completion_tokens": fields.get("completion_tokens", 0),
        "token_count": fields.get("token_count", 0),
        "cost_estimate": fields.get("cost_estimate", 0.0),
    }
    (usage_dir / name).write_text(json.dumps(record), encoding="utf-8")


def test_poll_usage_aggregates_one_file(tmp_path: Path):
    usage = tmp_path / "data" / "llm_usage"
    _write_usage(usage, "llm_usage_1.json", prompt_tokens=4991,
                 completion_tokens=159, token_count=5150, cost_estimate=0.000422)

    state = SessionState()
    state.poll_usage(tmp_path)

    assert state.tokens_in == 4991
    assert state.tokens_out == 159
    assert state.tokens_total == 5150
    assert abs(state.cost_estimate_total - 0.000422) < 1e-9
    assert state.provider == "gemini"
    assert state.model == "gemini-2.5-flash"


def test_poll_usage_is_incremental(tmp_path: Path):
    usage = tmp_path / "data" / "llm_usage"
    _write_usage(usage, "llm_usage_1.json", prompt_tokens=100,
                 completion_tokens=10, token_count=110, cost_estimate=0.001)

    state = SessionState()
    state.poll_usage(tmp_path)
    assert state.tokens_in == 100

    # Second poll with the SAME file must not double-count.
    state.poll_usage(tmp_path)
    assert state.tokens_in == 100

    # A new file IS counted on the next poll.
    _write_usage(usage, "llm_usage_2.json", prompt_tokens=50,
                 completion_tokens=5, token_count=55, cost_estimate=0.0005)
    state.poll_usage(tmp_path)
    assert state.tokens_in == 150
    assert state.tokens_out == 15
    assert abs(state.cost_estimate_total - 0.0015) < 1e-9


def test_poll_usage_live_counting_grows_monotonically(tmp_path: Path):
    """Live token-counting contract: polling per LLM call (as the feed callback now
    does) makes the totals grow as each usage file lands within the SAME turn —
    never jumping only at turn end, never double-counting an already-seen file."""
    usage = tmp_path / "data" / "llm_usage"
    state = SessionState()

    # No files yet → zero.
    state.poll_usage(tmp_path)
    assert state.tokens_total == 0

    seen_total = 0
    for i, (pt, ct, tc, cost) in enumerate(
        [(100, 10, 110, 0.001), (200, 20, 220, 0.002), (50, 5, 55, 0.0005)], start=1
    ):
        _write_usage(usage, f"llm_usage_{i}.json", prompt_tokens=pt,
                     completion_tokens=ct, token_count=tc, cost_estimate=cost)
        before = state.tokens_total
        state.poll_usage(tmp_path)          # per-event poll
        state.poll_usage(tmp_path)          # a second repaint poll must NOT double-count
        seen_total += tc
        assert state.tokens_total == seen_total
        assert state.tokens_total > before  # monotonic growth within the turn


def test_poll_usage_missing_dir_is_noop(tmp_path: Path):
    state = SessionState()
    state.poll_usage(tmp_path / "nonexistent")  # no crash
    assert state.tokens_in == 0


def test_poll_usage_skips_malformed(tmp_path: Path):
    usage = tmp_path / "data" / "llm_usage"
    usage.mkdir(parents=True, exist_ok=True)
    (usage / "llm_usage_bad.json").write_text("{not json", encoding="utf-8")
    _write_usage(usage, "llm_usage_good.json", prompt_tokens=7,
                 completion_tokens=1, token_count=8, cost_estimate=0.0)

    state = SessionState()
    state.poll_usage(tmp_path)
    assert state.tokens_in == 7


def test_agent_registration_sets_main_agent():
    state = SessionState()
    reg = normalize({
        "type": "agent_registration",
        "data": {"agent_id": "executor_main", "agent_name": "executor",
                 "model_name": "gemini-2.5-flash"},
    })
    assert isinstance(reg, AgentRegistration)
    state.update(reg)
    assert state.main_agent_id == "executor_main"
    assert state.model == "gemini-2.5-flash"
    # Main agent is not a sub-agent; a different id is.
    assert state.is_sub_agent("executor_main") is False
    assert state.is_sub_agent("researcher_42") is True


def test_agent_end_counts_errors():
    state = SessionState()
    end = normalize({
        "type": "agent_end",
        "data": {"agent_id": "x", "steps": 2, "success": False,
                 "errors": ["e1", "e2"]},
    })
    assert isinstance(end, AgentEnd)
    state.update(end)
    assert state.errors == 2
