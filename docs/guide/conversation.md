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
  whichever is smaller, at most 6000 characters). Your messages and the
  agent's replies come first; a command result, a transaction notice, a decision or
  a background report is one short line.
- A warm session sees the **delta** on every later turn: what other sessions
  and background runs exchanged with you since its own last turn. A long
  delta keeps the newest lines and gives a count of the rest. Your own
  commands, button taps and their results show as yours, never as the agent's or
  a background run's. An empty delta is no message.
- A Telegram **quote-reply** names the exact line you answered, even when that
  line came from a run this session never saw.
- A background run (a goal or a job) sees its own **slice**: the last thing it
  told you and what you replied to it.

So a reply to something the agent said three minutes ago lands with its
context, whichever session answers. The idle reset stays what it was — a clean
workspace and a fresh context window — but it is no longer a memory cliff.

## One main chat, and separate chats beside it

A direct chat on a chat app has one **main chat**: it is continuous, and it is where
you are until you choose another. For a subject you want apart from it, open a
separate chat:

| You send | What happens |
|---|---|
| `/new [name]` | opens a separate chat and puts you in it. The chat you left stays open and keeps its work |
| `/chats` | lists your chats: the main one first, each with a number and a tap |
| `/chat <number or name>` | puts you in that chat (`/chat main` goes back) |
| `/rename <name>` | names the chat you are in |
| `/close` | closes the separate chat you are in and puts you back in the main chat |

The terminal has the same chats and the same five commands, with one difference: a
terminal run stays in the chat it started in, so a switch there applies to your next
start (`polyrob --new` and `polyrob --chat <number or name>` choose it at the start).
`/chats` also lists the chats you have in the other places you talk to the agent.

Your next line always goes to the chat you are in. A line that arrives from a chat you
are not in starts with that chat's name in brackets, for example `[main]`. The main chat
does not close. A group chat has one conversation and no separate chats.

`/thread [n | <hours>h]` shows it on Telegram, in the REPL and in the
console, every line labelled with the run it came from. A group chat refuses
it. `/status` reports how many lines were recorded in the last day.

## The five regimes

| Regime | Where | Session lifetime | Reads the thread | Writes the thread |
|---|---|---|---|---|
| **Thread** (default) | a direct chat on Telegram or another chat app | each chat starts fresh after an idle gap (the agent tells you once); `/new` opens a separate chat | tail on the first turn, delta after | every reply |
| **One chat each** | the console (each new chat), `polyrob run` | until you close it; `polyrob run --continue <id> "message"` continues one | tail on the first turn, delta after | every reply |
| **Terminal run** | `polyrob` | one run; the next start in the same chat gets its summary | tail on the first turn, delta after | every reply |
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

## What the agent did

The agent reads exact counts and recent lines of every decision you made and each act outside from its
stores. This survives compaction and a fresh chat, and never reaches a group or another person.

## Switches

`OWNER_THREAD_INJECT` (on by default) reads the thread into sessions: the tail,
the delta, the referent and the slice. `OWNER_THREAD_TAIL_HOURS` and
`OWNER_THREAD_TAIL_ROWS` set the tail window. Their defaults are in
[`docs/CONFIGURATION.md`](../CONFIGURATION.md).

The thread is not the memory provider: `MEMORY_BACKEND` does not affect it.
A missing or unreadable store costs the context block, never a turn or a send.

See also: [Owner controls](owner-controls.md), [Group chats](groups.md),
[Configuration](configuration.md), and [Proof per rail](rails-verification.md) for what
counts as proof that a message the agent sent arrived.
