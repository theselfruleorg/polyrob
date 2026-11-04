---
name: goal-run
description: Inside a goal or scheduled-job run - read your own goal first, work to its acceptance, ask the owner the durable way, stay inside what a run may do and its budget, and report with evidence.
license: MIT
metadata:
  polyrob-when: 'Running a goal or job, or the run is blocked'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["goal","cronjob"]}'
  polyrob-version: '1'
---
# Goal run

Nobody is live in this run. The goal or job text is your whole brief, and the
owner reads only what you send and what the board records.

## 1. Read your contract first
- A goal: `goal_show(goal_id=<this goal>)` — acceptance, typed checks, failures,
  the last failure, what it waits on. Do not redo an attempt that already failed
  the same way.
- A job: the task text and any `<precheck>` block (use that read; do not read the
  same number again).
- `recent_activity(kind="goal")` or `(kind="cron")` shows what the last runs did
  (when the tool is present).

## 2. Work to the acceptance
- Plan in a few `task_todo_add` steps. On a long run, one `send_message` with the
  plan is enough.
- Prove the effect before you finish: read back the file, open the URL, quote the
  sent id. A run that cannot verify says "unverified" and why.

## 3. When you need the owner
- `goal_ask(what=…, why=…, blocks_goal_ids=[<this goal>])` holds THIS goal until the
  owner answers. A run may hold only its own goal.
- Any other decision: `owner_ask(question=…, why=…)`. Never a question in a
  message: the run cannot wait for a reply.
- Calling the same ask again refreshes it; it does not make a second one. Then
  keep the conservative reading and do the work that does not depend on it.

## 4. What a run may not do
- `goal_update`, `goal_unblock` and `goal_cancel` refuse here. Name the owner's
  verb instead: `/goal retry <id>`, `/goal grant <id> tools=…`, `/goal stop <id>`.
- `autonomy_control`, `present_choice` and a `forever=true` stop are the owner's.
- A missing or refused tool: read `agent_status(tool="<id>")`, then follow the
  pinned `harness-manual`. Never route around a gate.
- New standing work: `goal_create` is allowed; a new job only under the autonomous
  regime (the `standing-work` skill).

## 5. The budget
- The run stops at its step cap and at `AUTONOMOUS_RUN_BUDGET_USD` (see the
  environment block). Sub-agents count toward it.
- Near the cap, stop and report what is done, what is left and the next step,
  rather than let the cap cut the run mid-step.

## 6. Report
- The last `send_message` is the report: the result with its evidence (id, path,
  URL, figure from a tool), and what failed or could not be read.
- Nothing new on a watch or a repeat run: send nothing.
- `done` is the record, not the message.
