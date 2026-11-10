---
name: rule-conflict-check
description: Read-only check that finds where an active owner rule and an enabled job text or pinned skill disagree, and reports each conflict with both quotes. Writes nothing.
license: MIT
metadata:
  polyrob-when: 'A job that checks owner rules against job text'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["cronjob"]}'
  polyrob-version: '1'
---
# Rule conflict check

One rule can say a thing is forbidden while a job text or a pinned skill keeps doing it.
This check finds those pairs and tells the owner. It changes nothing.

## Set it up (owner's turn)

The owner creates the job; none ships. Suggest one run a week on a low-cost model:
`cronjob_schedule(schedule="every 7d", skills=["rule-conflict-check"], deliver=<the owner's
channel>, task="Run the rule conflict check and report what it finds.")`.

## Run it

1. Call `owner_doc_manage(action='sweep_all')`. It compares every active rule with enabled job
   text, live goal text and pinned skills, and returns each candidate with both quotes. It is
   code: it costs no model call.
   If that action is unavailable, read the `## Owner facts` block in your context, then
   `cronjob_list` and `cronjob_show` for the job texts, and `load_skill` for each pinned skill.
2. Read each candidate. A candidate shares words, so it may not be a conflict. Keep it only when
   following one text means breaking the other. Use `cronjob_show` or `load_skill` for the text
   around a quote.
3. Read the job texts once more for a conflict the words do not show, such as a limit in a rule
   and a larger amount in a job.
4. A line the check names as "holds a job's state" is a conflict with the job row. The job row
   wins: report the line, and do not ask which one is true.

## Report it

Send one message with `send_message`; the job's `deliver` channel carries it to the owner. For
each conflict give:

- the owner rule, quoted exactly;
- the job or skill that disagrees, by id, quoted exactly;
- one sentence on why they disagree.

Name each source you could not read. An unread source is not a clean one. With no conflict, send
one line that gives the counts of rules, jobs and skills read.

## Write nothing

This run reports only. Do not call a write action of `owner_doc_manage`, `cronjob_edit`,
`cronjob_cancel` or `skill_manage`. The owner decides each change. To fix a conflict, the owner
uses `replace_rule` for a rule and `cronjob_edit` for a job, on the owner's own turn.
