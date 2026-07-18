"""Wave 2 Task 2 — batch rollout runner."""
import asyncio
import json
from pathlib import Path

import pytest

from datagen.batch_runner import load_tasks, run_batch, scan_completed


class _FakePM:
    def __init__(self, root):
        self.data_root = Path(root)

    def get_session_root(self, session_id, user_id=None):
        # Canonical LIVE layout (agents/task/path.py): directly under the
        # user, NO "sessions" subdirectory — the fixture must match reality
        # or layout bugs in the assemble readers stay invisible (B-1/B-2).
        return self.data_root / (user_id or "anon") / session_id


class _FakeTaskAgent:
    """Stub honoring the run_task_to_outcome contract."""

    def __init__(self, pm, *, hang=False):
        self._pm = pm
        self._hang = hang
        self.requests = []
        self._n = 0

    async def create_session(self, user_id, request, **kwargs):
        self.requests.append(request)
        self._n += 1
        sid = f"bsess-{self._n}"
        root = self._pm.get_session_root(sid, user_id)
        hist = root / "memory" / "message_history.json"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(json.dumps({
            "session_id": sid, "saved_at": "t",
            "messages": [
                {"type": "HumanMessage", "content": request["task"],
                 "origin": "USER"},
                {"type": "AIMessage", "content": "done it"},
            ]}))
        # The step ledger lands where history_io writes it: data/history/.
        ledger = root / "data" / "history" / "agent_history_task.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"history": [{
            "model_output": {"current_state": {"next_goal": "finish"},
                             "action": [{"done": {}}]},
            "result": [{"extracted_content": "ok", "error": None}],
        }]}))
        return {"id": sid}

    async def run_session(self, user_id, session_id):
        if self._hang:
            await asyncio.sleep(30)
        return "Session completed successfully"


def _tasks_file(tmp_path, prompts):
    p = tmp_path / "tasks.jsonl"
    p.write_text("\n".join(json.dumps({"prompt": t}) for t in prompts))
    return p


def test_load_tasks_requires_prompt(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        load_tasks(p)


def test_run_batch_writes_rollouts_corpus_stats(tmp_path, monkeypatch):
    fake_pm = _FakePM(tmp_path / "data")
    monkeypatch.setattr("datagen.batch_runner.pm", lambda: fake_pm)
    agent = _FakeTaskAgent(fake_pm)
    tasks = load_tasks(_tasks_file(tmp_path, ["say hi", "say bye"]))
    run_dir = tmp_path / "run1"

    stats = asyncio.run(run_batch(agent, tasks, run_dir, max_run_seconds=5,
                                  concurrency=2, seed=1))
    assert stats["completed"] == 2
    rollouts = sorted(run_dir.glob("rollout_*.json"))
    assert len(rollouts) == 2
    payload = json.loads(rollouts[0].read_text())
    assert payload["provenance"]["source"] == "datagen_batch"
    assert payload["provenance"]["prompt_text"] in ("say hi", "say bye")
    assert payload["provenance"]["toolsets_used"]
    # REGRESSION (B-1/B-13): a successful rollout must carry the step-level
    # ledger — the wrong-directory reader exported steps=[] for every real
    # session and the failure was invisible in the mocked fixtures.
    assert payload["steps"], "rollout is missing the agent step ledger"
    corpus = (run_dir / "corpus.jsonl").read_text().strip().splitlines()
    assert len(corpus) == 2
    assert "conversations" in json.loads(corpus[0])
    assert (run_dir / "statistics.json").exists()
    # request shape mirrors the goal dispatcher contract
    req = agent.requests[0]
    assert set(req) >= {"task", "tools", "max_steps", "temperature"}


def test_run_batch_resume_skips_completed(tmp_path, monkeypatch):
    fake_pm = _FakePM(tmp_path / "data")
    monkeypatch.setattr("datagen.batch_runner.pm", lambda: fake_pm)
    agent = _FakeTaskAgent(fake_pm)
    tasks = load_tasks(_tasks_file(tmp_path, ["say hi", "say bye"]))
    run_dir = tmp_path / "run1"
    asyncio.run(run_batch(agent, tasks, run_dir, max_run_seconds=5, seed=1))
    calls_before = len(agent.requests)

    stats = asyncio.run(run_batch(agent, tasks, run_dir, max_run_seconds=5,
                                  seed=1, resume=True))
    assert len(agent.requests) == calls_before  # nothing re-ran
    assert stats["skipped_resume"] == 2


def test_run_batch_timeout_counted(tmp_path, monkeypatch):
    fake_pm = _FakePM(tmp_path / "data")
    monkeypatch.setattr("datagen.batch_runner.pm", lambda: fake_pm)
    agent = _FakeTaskAgent(fake_pm, hang=True)
    tasks = load_tasks(_tasks_file(tmp_path, ["slow one"]))

    stats = asyncio.run(run_batch(agent, tasks, tmp_path / "run2",
                                  max_run_seconds=0.2, seed=1))
    assert stats["failed"] == 1
    assert stats["completed"] == 0


def test_run_batch_assemble_failure_counted_not_fatal(tmp_path, monkeypatch):
    """REGRESSION (B-13): an assemble/export exception escaped through
    asyncio.gather (no return_exceptions) and aborted the WHOLE batch."""
    fake_pm = _FakePM(tmp_path / "data")
    monkeypatch.setattr("datagen.batch_runner.pm", lambda: fake_pm)

    def boom(*args, **kwargs):
        raise RuntimeError("assemble boom")

    monkeypatch.setattr("datagen.batch_runner.assemble_record", boom)
    agent = _FakeTaskAgent(fake_pm)
    tasks = load_tasks(_tasks_file(tmp_path, ["say hi", "say bye"]))
    stats = asyncio.run(run_batch(agent, tasks, tmp_path / "run3",
                                  max_run_seconds=5, seed=1))
    assert stats["failed"] == 2
    assert stats["completed"] == 0


def test_scan_completed_reads_prompt_text(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "rollout_0.json").write_text(json.dumps({
        "provenance": {"prompt_text": "say hi"}}))
    assert scan_completed(run_dir) == {"say hi"}
