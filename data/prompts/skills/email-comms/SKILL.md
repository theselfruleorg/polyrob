---
name: email-comms
description: Compose, triage and reply to email and short comms
license: MIT
metadata:
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["email","send a message","reply to","draft email","follow up","compose email","inbox"],"task_patterns":["(send|draft|reply|compose).*email","follow up.*with","reply.*to"],"tool_ids":[]}'
  polyrob-version: '2'
---
# Email & Comms

Compose, triage, and reply to email and short messages. Recommended workflow —
adapt it. Uses the `email` tool.

## Tool availability
The `email` tool ships in the server default tool set; it is **not loaded in the CLI
by default**. If `email` isn't available, draft the message and hand it back rather
than failing.

## When to use
Sending, drafting, or replying to email; following up; triaging an inbox/thread.

## Workflow
1. **Confirm recipient, subject, and intent** before composing.
2. **Draft, then get the owner's decision with a tool, not in prose.** Never
   auto-send sensitive or external comms. In a live chat turn, show the draft in
   `send_message` and ask with `present_choice(question="Send this email to …?",
   options=["Send", "Edit", "Discard"])`. In an autonomous (goal/cron) run,
   nobody can answer now: raise `owner_ask(question="Send the draft to …? A) send
   B) discard", why=…)`, save the draft, and send on the next run only if the
   answer is "send". Do not make a typed "ok" in chat the send gate — the
   tap is the gate.
3. **Match the register** (formal vs casual) to the relationship and context.
4. **Keep it short** and lead with the ask; one clear call to action.
5. **Triage:** summarize the thread before replying so the reply is in context.

## Notes
- Never put secrets, credentials, or tokens in an email body.
