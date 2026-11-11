---
name: standing-work
description: Choose between a TODO, a goal, a scheduled job, a background delegation and a one-time reminder; shape a good goal or job; use the $0 watch; change work in place, never re-create it.
license: MIT
metadata:
  polyrob-when: 'Setting up a goal, a scheduled job or a reminder'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["goal","cronjob"]}'
  polyrob-version: '3'
---
# Standing work

Work that outlives this turn has one right home. Pick it first, then shape it so a
later run, which sees only what you wrote, can finish it.

## 1. Choose the home

| The work | Home | Verb |
|---|---|---|
| Steps of this turn | a TODO | `task_todo_add` |
| One piece of work, done once, in the background | a goal | `goal_create` |
| Pay people, or many reads — asked in the owner's chat | a heavy goal | `goal_create(tier="heavy", why=…, report_back=true)` |
| The same work again at set times, or a watch | a job | `cronjob_schedule` |
| One reminder at one time | a one-time job | `cronjob_schedule(schedule="<ISO time ending in Z>")` |
| A long side task for this chat only | a background child | `delegate_task(background=true)` (lost on a restart) |

- A goal runs ONCE. A cadence in its text ("every day", "per run") is a job; the
  `goal_create` result warns when it reads like one — fix it now.
- A goal has no start time: it starts when a slot is free, within the goal limits.
  "At 15:00" is a one-time job.
- `30m`, `2h`, `1d` REPEAT. "Remind me in 2 hours" is an ISO time two hours from
  now, not `2h`.
- A reminder's task says "send the owner X with `send_message`": `deliver` only
  copies that message.

## 2. A good goal

- `title` short; `body` = the full brief (facts, paths, the output shape).
- `acceptance` = an effect outside the goal board (a file, a URL, a sent id). An
  acceptance that reads the board back is refused.
- `acceptance_checks` only from the four typed shapes in the schema; use
  `file_contains` only for an exact string.
- `tools` = every tool the run needs, or a narrow `rig`. A money tool is the
  owner's grant (`/goal grant <id> tools=…`), never yours.
- `skills=[…]` pins the doctrine the run must follow; `depends_on` orders goals;
  `report_back` defaults true for a heavy or money goal from the owner's chat; else a one-line notice.

## 3. A good job

- `task` names what to do and what counts as news. Say "send nothing when nothing
  changed" for a watch.
- `schedule`: the slowest cadence that still catches the change. Every tick that
  runs the model costs a model run.
- Clock: `every day 09:00`, a 5-field cron and an ISO time with no zone use the
  server clock; an ISO time with `Z` is UTC. The owner's `digest.timezone` does not
  move a schedule: convert an hour the owner gives in a local zone, and name both
  times.
- Floor: a job that runs the model repeats every 5 minutes at most.
- One slot, one model run. Jobs run one at a time, so a job in a taken slot starts
  only after the job before it ends. The tool moves a new time to the nearest free
  minute and the result names the move: tell the owner the new time. Set
  `exact_time=true` only when the owner named the minute or the work must start
  then.
- `max_duration_seconds`, `deliver` (`telegram` or `email` to the owner; `twitter`
  is a PUBLIC post), `skills=[…]`, a narrow `rig`, or exact `tools=[…]` when no rig
  fits.
- An end: a one-time time, or a stop condition in the task text.
- Limits the scheduler holds: a recurring job pauses after 3 failed runs in a row, and
  the owner gets one line (`/job resume <id>`). A model run that a crash cut does not
  repeat by itself. A run more than 30 minutes late says so in its task;
  a money job more than 1 hour late does not run, unless it is an exit.
- Jobs in order: a job cannot wait for another job, and only goals have
  `depends_on`. Give the later job a later time, by at least the earlier job's
  `max_duration_seconds`: `every 3d from <T>`, then `every 3d from <T + offset>`.

## 4. The $0 watch

For an on-chain number, the tick can run with no model call:
- `read_verb={verb, params, alert, deliver_on:"alert"}` — the verb reads, the code
  compares the `alert` bound, and only an ALERT or an error reaches the owner.
- `precheck={verb, params, alert?, move_pct?, field?, max_quiet_hours?}` — a quiet
  read skips the tick at $0; a crossed bound or a move runs the model with the read
  attached.
- ⚠️ Only the `defi_data` public reads named in the `read_verb` schema can run this
  way. A web page, a feed or any other source runs the model on every tick: tell the
  owner the cost per tick and pick a slow cadence.
- `write_verb` is set only on the owner's own turn.

## 5. Change it in place

- `cronjob_show` → `cronjob_edit` (`append_text`, or `old_text` → `new_text`, or one
  field). Never cancel and re-create a job.
- `cronjob_list` marks a job that overlaps another and names a free slot. On the
  owner's turn, move it with `cronjob_edit` and name the move; do not ask first. If
  its time must stay, set `exact_time=true`.
- `cronjob_run_now` queues one run on the next scheduler pass. Off an owner turn it
  needs the autonomous regime, and it refuses a money job or a run inside its
  schedule's gap.
- A goal: `goal_show`, then `goal_update` on the owner's own turn. Elsewhere, name
  the owner's verb (`/goal retry <id>`, `/goal grant <id> tools=…`).

## 6. Who may do what

- The owner's own turn: everything here.
- A goal or cron run: `goal_create`; a job only when the instance runs the
  autonomous regime; `goal_update`, `goal_unblock` and `goal_cancel` refuse.
- A leaf (a delegated child): none of it — return the proposal to the parent.
- `forever=true` on `goal_cancel` / `cronjob_cancel` only on the owner's word.

## 7. Report it

Quote the id, the next run and what each run costs; name how the owner stops it
(`/job stop <id>`, `/goal stop <id>`). A dropped tool or a warning in the result is a
fix to make now, not a footnote.
