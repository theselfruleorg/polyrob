"""``polyrob datagen`` — training-data corpus commands (parity W1 T5 / W2 T3).

Bulk, label-filtered trajectory export + the batch rollout runner. Pure logic
lives in ``datagen/bulk_export.py`` / ``datagen/batch_runner.py``; this is the
thin click wrapper.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

import click

#: Trajectory-hygiene env for `datagen run` (set BEFORE container build):
#: no cross-session memory, no project-context injection, no autonomy loops,
#: and no double capture (the runner writes its own records).
_DATAGEN_HYGIENE_ENV = {
    "MEMORY_BACKEND": "none",
    "PROJECT_CONTEXT_AUTOLOAD": "false",
    "SELF_WAKE_ENABLED": "false",
    "GOALS_ENABLED": "false",
    "BACKGROUND_REVIEW_ENABLED": "false",
    "TRAJECTORY_CAPTURE": "false",
}


@click.group("datagen")
def datagen():
    """Training-data collection (trajectory corpus) commands."""


def _parse_filters(pairs: tuple[str, ...]) -> dict:
    filters = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(
                f"--filter takes key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        filters[key.strip()] = value.strip()
    return filters


@datagen.command("export")
@click.option("--output", "-o", type=click.Path(), default="corpus.jsonl",
              show_default=True, help="Output JSONL path.")
@click.option("--format", "fmt",
              type=click.Choice(["raw", "sharegpt", "openai"]),
              default="sharegpt", show_default=True)
@click.option("--filter", "filters", multiple=True,
              help="Label filter key=value (repeatable), e.g. outcome=done")
@click.option("--user-id", default=None, help="Only this tenant's sessions.")
@click.option("--include-correspondent", is_flag=True, default=False,
              help="Include sessions containing third-party correspondent "
                   "messages (excluded by default).")
@click.option("--limit", type=int, default=None, help="Max sessions to export.")
def datagen_export(output, fmt, filters, user_id, include_correspondent, limit):
    """Export a label-filtered trajectory corpus from local sessions."""
    from agents.task.path import pm
    from datagen.bulk_export import bulk_export

    try:
        parsed = _parse_filters(filters)
    except click.BadParameter as e:
        click.echo(click.style("[polyrob] ", fg="red") + str(e))
        sys.exit(2)

    stats = bulk_export(pm().data_root, output, fmt, parsed,
                        include_correspondent=include_correspondent,
                        limit=limit, user_id=user_id)
    click.echo(click.style("[polyrob] ", fg="green")
               + f"Exported {stats['exported']} session(s) to {output}")
    detail = ", ".join(f"{k.replace('skipped_', '')}={v}"
                       for k, v in stats.items()
                       if k.startswith("skipped_") and v)
    if detail:
        click.echo(f"  skipped: {detail}")


@datagen.command("run")
@click.option("--tasks", "tasks_path", required=True,
              type=click.Path(exists=True), help="JSONL task list "
              '(rows: {"prompt": ..., "tools"?, "model"?, "max_steps"?}).')
@click.option("--name", default=None,
              help="Run name (default: datagen-<timestamp>).")
@click.option("--distribution", default="default", show_default=True,
              help="Toolset distribution (see datagen/toolset_distributions.py).")
@click.option("--concurrency", type=int, default=2, show_default=True)
@click.option("--max-steps", type=int, default=12, show_default=True)
@click.option("--max-run-seconds", type=float, default=600.0, show_default=True,
              help="Per-rollout wall-clock cap.")
@click.option("--model", default=None)
@click.option("--provider", default=None)
@click.option("--user-id", default="datagen", show_default=True)
@click.option("--seed", type=int, default=0, show_default=True)
@click.option("--no-resume", is_flag=True, default=False,
              help="Re-run prompts that already have rollouts in the run dir.")
def datagen_run(tasks_path, name, distribution, concurrency, max_steps,
                max_run_seconds, model, provider, user_id, seed, no_resume):
    """Run a batch of tasks as agent rollouts and write a labeled corpus."""
    from datagen.toolset_distributions import DISTRIBUTIONS

    if distribution not in DISTRIBUTIONS:
        click.echo(click.style("[polyrob] ", fg="red")
                   + f"Unknown distribution {distribution!r}. Known: "
                   + ", ".join(sorted(DISTRIBUTIONS)))
        sys.exit(2)

    for key, value in _DATAGEN_HYGIENE_ENV.items():
        os.environ.setdefault(key, value)

    run_name = name or f"datagen-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    stats = asyncio.run(_datagen_run_async(
        tasks_path, run_name, distribution, concurrency, max_steps,
        max_run_seconds, model, provider, user_id, seed, not no_resume))
    click.echo(click.style("[polyrob] ", fg="green")
               + f"Run {run_name}: {stats['completed']}/{stats['total']} "
                 f"completed, {stats['failed']} failed, "
                 f"${stats.get('spend_usd', 0.0)} spent")


async def _datagen_run_async(tasks_path, run_name, distribution, concurrency,
                             max_steps, max_run_seconds, model, provider,
                             user_id, seed, resume) -> dict:
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)
    setup_project_path()
    setup_sqlite_compat()
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    _logging.disable(_logging.CRITICAL)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    from agents.task.path import pm
    from cli.commands.session import task_agent_or_none
    from datagen.batch_runner import load_tasks, run_batch

    task_agent = task_agent_or_none(container)
    if task_agent is None:
        click.echo(click.style("[polyrob] ", fg="red")
                   + "Task agent unavailable in this container")
        sys.exit(1)

    tasks = load_tasks(tasks_path)
    run_dir = pm().data_root / "datagen" / "runs" / run_name
    click.echo(f"  {len(tasks)} task(s) -> {run_dir}")
    return await run_batch(
        task_agent, tasks, run_dir, distribution=distribution,
        user_id=user_id, concurrency=concurrency, max_steps=max_steps,
        max_run_seconds=max_run_seconds, model=model, provider=provider,
        seed=seed, resume=resume)
