"""Batch rollout runner — JSONL tasks → labeled trajectory corpus (design §A2).

Runs each task on the SAME session rail goals/cron use
(``run_task_to_outcome``), with per-task toolset sampling
(``datagen.toolset_distributions``), a concurrency semaphore, a per-rollout
wall-clock cap, and content-based resume (a prompt whose exact
text already has a rollout in the run dir is skipped).

Outputs, under ``run_dir``:
- ``rollout_<i>.json``   — canonical (raw) record per completed task, labeled
  from its RunOutcome, scrubbed fail-closed (scrub failure → skipped+counted).
- ``corpus.jsonl``       — sharegpt render of every completed rollout.
- ``statistics.json``    — outcome counts, spend, tool usage.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional

from agents.task.path import pm

from datagen.assemble import assemble_record
from datagen.capture import outcome_labels
from datagen.formats import render_raw, render_sharegpt
from datagen.scrub import ScrubError, has_correspondent_content, scrub_record
from datagen.toolset_distributions import sample_toolsets

logger = logging.getLogger(__name__)


def load_tasks(path: Path) -> list[dict]:
    """Load a JSONL task list; every row must carry a non-empty ``prompt``."""
    tasks = []
    for lineno, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not str(row.get("prompt") or "").strip():
            raise ValueError(f"{path}:{lineno}: task rows need a 'prompt' field")
        tasks.append(row)
    return tasks


def scan_completed(run_dir: Path) -> set[str]:
    """Prompt texts that already have a rollout record (content-based resume)."""
    done = set()
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return done
    for f in run_dir.glob("rollout_*.json"):
        try:
            prov = json.loads(f.read_text()).get("provenance") or {}
            text = prov.get("prompt_text")
            if text:
                done.add(text)
        except (json.JSONDecodeError, OSError):
            continue
    return done


async def _run_one(task_agent: Any, task: dict, index: int, run_dir: Path,
                   *, distribution: str, user_id: str, max_steps: int,
                   max_run_seconds: float, model: Optional[str],
                   provider: Optional[str], rng: random.Random,
                   stats: dict) -> None:
    from agents.task.runtime.run_as_session import run_task_to_outcome

    prompt = str(task["prompt"])
    tools = list(task.get("tools") or sample_toolsets(distribution, rng))
    request: dict = {
        "task": prompt,
        "tools": tools,
        "max_steps": int(task.get("max_steps") or max_steps),
        "temperature": 0.0,
    }
    if task.get("model") or model:
        request["model"] = task.get("model") or model
    if task.get("provider") or provider:
        request["provider"] = task.get("provider") or provider

    try:
        outcome = await asyncio.wait_for(
            run_task_to_outcome(task_agent, user_id=user_id, request=request,
                                autonomous=True),
            timeout=max_run_seconds)
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        logger.warning("datagen run: task %d failed: %s", index, e)
        stats["failed"] += 1
        return

    if not outcome.session_id or outcome.refusal:
        logger.warning(
            "datagen run: task %d produced no usable session "
            "(session_id=%r, refusal=%r)", index, outcome.session_id,
            getattr(outcome, "refusal", None))
        stats["failed"] += 1
        return

    # Assemble/export faults must be counted per-task, not propagate — an
    # unhandled exception here escaped through asyncio.gather (no
    # return_exceptions) and aborted the ENTIRE batch.
    try:
        session_dir = pm().get_session_root(outcome.session_id, user_id)
        record = assemble_record(Path(session_dir),
                                 session_meta={"model": request.get("model"),
                                               "provider": request.get("provider"),
                                               "task": prompt},
                                 labels=outcome_labels(outcome),
                                 user_id=user_id)
        record.session_id = str(outcome.session_id)
        record.provenance.update({
            "source": "datagen_batch",
            "prompt_index": index,
            "prompt_text": prompt,
            "toolsets_used": tools,
            "distribution": distribution,
        })
        if has_correspondent_content(record):
            stats["skipped_correspondent"] += 1
            return
        try:
            scrub_record(record)
        except ScrubError:
            logger.warning("datagen run: scrub failed for task %d — skipped",
                           index)
            stats["skipped_scrub"] += 1
            return

        (Path(run_dir) / f"rollout_{index}.json").write_text(
            json.dumps(render_raw(record), default=str))
        stats["completed"] += 1
        stats["_spend"] += float(record.labels.get("spend_usd") or 0.0)
        for t in tools:
            stats["_tools"][t] = stats["_tools"].get(t, 0) + 1
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("datagen run: task %d assemble/export failed", index,
                       exc_info=True)
        stats["failed"] += 1


async def run_batch(task_agent: Any, tasks: list[dict], run_dir: Path, *,
                    distribution: str = "default", user_id: str = "datagen",
                    concurrency: int = 2, max_steps: int = 12,
                    max_run_seconds: float = 600.0,
                    model: Optional[str] = None,
                    provider: Optional[str] = None,
                    seed: int = 0, resume: bool = True) -> dict:
    """Run every task; returns the stats dict (also saved to statistics.json)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    done_texts = scan_completed(run_dir) if resume else set()
    stats: dict = {"total": len(tasks), "completed": 0, "failed": 0,
                   "skipped_resume": 0, "skipped_scrub": 0,
                   "skipped_correspondent": 0, "_spend": 0.0, "_tools": {}}

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _guarded(task: dict, index: int) -> None:
        async with sem:
            await _run_one(task_agent, task, index, run_dir,
                           distribution=distribution, user_id=user_id,
                           max_steps=max_steps,
                           max_run_seconds=max_run_seconds, model=model,
                           provider=provider, rng=rng, stats=stats)

    pending = []
    for i, task in enumerate(tasks):
        if str(task["prompt"]) in done_texts:
            stats["skipped_resume"] += 1
            continue
        pending.append(_guarded(task, i))
    if pending:
        await asyncio.gather(*pending)

    # Merge corpus (sharegpt) from every rollout present in the run dir.
    with open(run_dir / "corpus.jsonl", "w") as corpus:
        for f in sorted(run_dir.glob("rollout_*.json")):
            try:
                payload = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            from datagen.record import TrajectoryRecord
            fields = {k: v for k, v in payload.items() if k != "schema_version"}
            corpus.write(json.dumps(
                render_sharegpt(TrajectoryRecord(**fields)), default=str) + "\n")

    summary = {k: v for k, v in stats.items() if not k.startswith("_")}
    summary["spend_usd"] = round(stats["_spend"], 6)
    summary["tool_usage"] = dict(sorted(stats["_tools"].items()))
    summary["distribution"] = distribution
    (run_dir / "statistics.json").write_text(json.dumps(summary, indent=2))
    stats.update(summary)
    return stats
