# One conversation, many sessions

You have one conversation with your agent. It runs on your phone, in the web
console and in the terminal, and it does not stop when a session ends. This
page names the four regimes every seat implements, what rolls when a session
rolls, and the switches behind it.

## The owner thread

Every line that reaches you — a chat reply, a cron report, an approval notice,
a `message` tool send from a background run — and every line you send on an
owner seat is recorded once, in one durable store (`owner_thread.db`, per
tenant, bounded). A session **reads** that thread; it never owns it.

- A fresh session sees the **tail** of the conversation on its first turn (the
  last `OWNER_THREAD_TAIL_HOURS` hours or `OWNER_THREAD_TAIL_ROWS` lines,
  whichever is smaller, at most 2000 characters).
- A warm session sees the **delta** on every later turn: what other sessions
  and background runs exchanged with you since its own last turn. A long
  delta keeps the newest lines and gives a count of the rest. Your own
  commands, button taps and their results show as yours, never as Rob's or
  a background run's. An empty delta is no message.
- A Telegram **quote-reply** names the exact line you answered, even when that
  line came from a run this session never saw.
- A background run (a goal or a job) sees its own **slice**: the last thing it
  told you and what you replied to it.

So a reply to something the agent said three minutes ago lands with its
context, whichever session answers. The idle reset and `/new` stay what they
were — a clean workspace and a fresh context window — but they are no longer
a memory cliff.

`/thread [n | <hours>h]` shows it on Telegram, in the REPL and in the
console, every line labelled with the run it came from. A group chat refuses
it. `/status` reports how many lines were recorded in the last day.

## The four regimes

| Regime | Where | Session lifetime | Reads the thread | Writes the thread |
|---|---|---|---|---|
| **Thread** (default) | Telegram DM, console landing page, REPL | rolls on idle, `/new`, or a daily reset | tail on the first turn, delta after | every reply |
| **Task session** | `/task …`, console `/c/<id>`, `polyrob run` | one session, explicit lifetime | tail on the first turn, delta after | every reply |
| **Room** | an allowlisted group chat | per room | **never** — the thread is private | never |
| **Autonomous run** | goals, jobs, approvals and settlements | one run | its own slice | every line that reached you |

## Decisions are asks

A background run cannot wait for your answer: the run ends before you reply,
and your reply lands in a chat session. So a decision it needs is an **ask**,
never a question in a message. The agent raises it with `owner_ask`; it is
durable, deduplicated, shown on every owner seat, and delivered to you once
with its id. Answer in chat ("A"), with the option button on the notice, or
with `/fulfill <id> <answer>`. A chat answer counts only when the words are
yours, typed in that message. The answer is handed to that work's next run,
once. `send_message(wait_for_response=true)`
from a background run is refused with this remedy.

## Switches

| Flag | Default | Meaning |
|---|---|---|
| `OWNER_THREAD_INJECT` | ON | read it into sessions (tail, delta, referent, slice) |
| `OWNER_THREAD_TAIL_HOURS` / `OWNER_THREAD_TAIL_ROWS` | 24 / 12 | the tail window |

The thread is not the memory provider: `MEMORY_BACKEND` does not affect it.
A missing or unreadable store costs the context block, never a turn or a send.

See also: [Owner controls](owner-controls.md), [Group chats](groups.md),
[Configuration](configuration.md).
