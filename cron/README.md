# Cron Package — durable scheduled runs

_Last reviewed: 2026-06-30. For env flags see ../docs/CONFIGURATION.md; for the autonomy loops see ../docs/guide/architecture.md and ../AGENTS.md._

## Overview

The `cron` package provides **durable, scheduled agent runs** — the home for work
that must survive a process restart, which in-memory `delegate_task` cannot. Jobs
are persisted in SQLite (`data/cron.db`, WAL + jittered retry) and executed by a
ticker that is safe under multiple workers via a file-based tick lock.

Off by default: nothing ticks unless `CRON_ENABLED=true`. It is wired into both the
API lifespan and the local CLI through the shared autonomy runtime
(`core/autonomy_runtime.py::start_autonomy`), alongside the goal board and curator.

## Package structure

```
cron/
├── schedule.py   # PURE schedule parser + next-run computation (tz-naive, local
│                 #   wall-clock, reference time passed in → deterministic):
│                 #   durations ("30m"), `every monday 09:00`, 5-field cron,
│                 #   ISO one-shot
├── jobs.py       # SQLite cron_jobs store — durable schedule state; WAL mode with
│                 #   jittered retry on write contention
├── service.py    # CronService — schedule / list / cancel API over schedule+jobs
│                 #   (thin, fully testable; shared by the tool and the ticker)
├── scheduler.py  # tick(): find due jobs → run each through an injected async
│                 #   runner under a hard per-run duration cap → reschedule
│                 #   (recurring) or complete (one-shot); file-based TickLock
├── runner.py     # CronTicker (drives tick() on an interval) + make_agent_runner
│                 #   (W3 fix: actually runs run_session, not just create_session)
└── delivery.py   # out-of-band result delivery after a run: allowlist
                  #   {telegram, email, twitter}; [SILENT] suppresses; tenant-scoped
                  #   recipient; fail-open; inside the scheduler wait_for budget
```

## Execution model

1. `CronTicker` calls `scheduler.tick()` on an interval.
2. `tick()` acquires the file `TickLock` (safe under `workers>1`), finds due jobs,
   and runs each through the injected runner with a hard duration cap.
3. `make_agent_runner` runs the real agent loop (`create_session` + `run_session`,
   `skip_memory=True`); a one-shot job is marked done, a recurring job is
   rescheduled to its next run.
4. If the job requested delivery, `delivery.py` sends the final result to the
   chosen sink (within the tick's `wait_for` budget).

## Key invariants

- **W3 run-loop fix (default on, `CRON_RUN_LOOP`):** cron historically called only
  `create_session` and never ran the loop, so jobs created an idle session and did
  nothing. It now runs `run_session`.
- **Idle-gate:** `scheduler.tick()` skips while the interactive REPL is busy
  (shared CWD safety — see `core/interactive_gate.py`); inert on the server.
- **Delivery is gated** separately (`CRON_DELIVERY_ENABLED`, default off) and
  fail-open.

## Related

- Agent-facing tool surface: `tools/cronjob_tools.py`
  (`cronjob_schedule` / `list` / `cancel`), registered only when `CRON_ENABLED=true`.
- Durable cross-session **goal board** (reuses this package's `TickLock` pattern):
  `agents/task/goals/` + `agents/task/goals/dispatcher.py`.
- Shared lifecycle: `core/autonomy_runtime.py`.
