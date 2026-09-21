# The console

The console is polyrob's web seat: a browser view of what the agent is doing, and
the controls to decide, pause and correct it. It runs as its own process beside the
agent and reads the same stores the agent itself uses — the goal board, cron, the
memory provider, the ledger, `polyrob doctor` — never a second copy of the numbers.

```bash
polyrob dashboard          # alias: polyrob webgate
```

That binds `127.0.0.1:5050` and opens a browser (`--no-browser` to skip,
`--host` / `--port` to change the bind). Which posture it runs in, how owner login
works, and how to run it as a service are in
[deployment-postures.md](deployment-postures.md) and
[self-hosting.md](self-hosting.md).

---

## The five destinations

| Nav | Route | What it answers |
|---|---|---|
| New | `/` | Talk to it. A thread you already started is `/c/{session_id}` |
| Inbox | `/inbox` | What is waiting on you, and what expires soonest |
| Work | `/work` | What it is doing and what is queued |
| Money | `/money` | What it earned, what it spent, what it holds |
| Agent | `/agent` | Who it is, how it is configured, whether it is healthy |

**New** opens on one true sentence about the last day — built from the same reader
`polyrob journey` and the chat `/recap` use — four things you could ask, and a place
to type. It is deliberately not a configuration form. A thread you do not own
answers 404 rather than an access-denied page.

On a public posture, `/` is also the status page an unauthenticated visitor sees:
"polyrob is live", the instance id, the version. Signing in replaces it with the
console.

**Inbox** is the one list of what is blocked on a person, composed from five stores:
the self-evolution review queue, tool approvals, correspondents awaiting your
approval, the agent's open asks, and pending app deployments. Each card says what it
wants, why, what yes costs, what no costs, and how long it has waited; Approve,
Reject and Fulfill act through exactly the same deciders as `polyrob owner pending`
and the chat verbs, so one decision means the same thing on every seat. A store that
would not answer is listed as an unreadable entry with its reason — never dropped,
and never counted as zero.


### Live sessions

`/c/{id}` streams a running session's transcript over Socket.IO as the agent works,
alongside its workspace file tree, file previews and downloads, and any browser
screenshots it captured. (`/session/{id}` is a permanent redirect to it.)

⚠️ **A session URL is not a grant.** On both authenticated postures a session page,
its feed and its workspace reads are owner-only — possessing the link gives you
nothing. Earlier builds let anyone holding a session URL read the transcript; that
is gone. There is one narrow bypass left in the code — an expiring preview token
scoped to a single session's `/serve/` subtree is still accepted — but nothing
hands one out: the endpoint that minted them was removed because no screen had
ever called it. So today there is no way to share a session at all.

---

## Every page leads with the same two facts

The frame above every destination states, once:

- **whether autonomy is paused**, read from the one pause record every loop
  honours — including which scopes and until when. An unreadable record renders as
  paused, because that is what the runtime then does. The header carries the
  **Pause/Resume** control itself, on every destination, so the sentence and the
  control that changes it are in the same place. It is drawn only on the owner's
  own console (`local` or `own_ops`) and never in read-only mode — pausing is
  instance-wide, so a multitenant tenant may not do it and is not shown a button
  that would refuse;
- **how much is in progress** when it is running, counted from running goals,
  running cron jobs and live sessions. If one of those stores did not answer the
  head line reads `N+`, never a bare `N` — the number is a floor, not a
  measurement;
- **whether this console can act at all** (the read-only badge, below).

The Inbox count rides in the nav. When a source could not be read it says "at
least N waiting, one list unreadable" rather than a confident number.

---

## What you can do here

The console is a control plane, not only a monitor. Unless it is read-only, you can:

- pause and resume autonomy, by scope and with a duration;
- approve or reject anything in the Inbox — and **answer** an ask in writing, not
  only agree to it, because an ask is a question ("which key should I use?") and
  "approved" is not an answer to one;
- act on goals and cancel cron jobs;
- settle an invoice (an attestation, not a payment);
- approve, reject or kill a durable app, and read its logs;
- write preferences and cataloged flags;
- type an owner verb into the chat box.

### Owner verbs in the chat box

A leading `/verb` is routed through the same owner-verb plane every other seat uses,
not sent to the model as prose. Roughly forty verbs work — `/pause`, `/resume`,
`/halt`, `/status`, `/inbox`, `/pending`, `/approve`, `/reject`, `/asks`, `/goals`,
`/cron`, `/book`, `/wallet`, `/invoices`, `/settle`, `/trade`, `/bridge`, `/launch`,
`/deploy`, `/apps`, `/mcp`, `/kb`, `/files`, `/groups`, `/mute`, `/prefs`,
`/config`, `/recap`, `/missed` and more. `/help` lists them.

This works from an empty chat box too, not only inside a thread you already have
open: a leading verb typed on the front door is answered inline and starts no
session. (It used to become a task, so `/halt` from a cold open started an agent
run whose job was the literal text `/halt`.)

`/task` and `/new` are the exception: the console has its own controls for starting
a session, so they are excluded here and `/help task` says so. Prose and an unknown
slash still reach the agent.

Full owner-control model: [owner-controls.md](owner-controls.md).

---

## Read-only mode

```bash
WEBVIEW_READ_ONLY=true
```

Every mutating endpoint then returns 403 server-side and the chat input is not
rendered. The frame says this once, in words, instead of showing you a screen of
greyed buttons. Owner login still works, so the monitoring seat is never lost.

Use it where the agent is driven elsewhere — Telegram, the CLI — and the console
only watches. Be aware that it disables *everything*, approvals included.

---

## What the console refuses

These are deliberate refusals with remedies, not bugs.

**It will not boot as an anonymous console on a server.** At `local` posture there
is no login and every anonymous request is the owner. If the process also looks
like a server — a public URL is configured, it runs behind a reverse proxy, or the
data directory is a system path — the console refuses to start and names what it
saw. Set a real posture, or `WEBVIEW_ALLOW_LOCAL_POSTURE=1` if you front it with
your own authentication.

**It will not boot as a writable console without a bound owner and a shared session
registry.** Both preconditions, and how to satisfy them, are in
[deployment-postures.md](deployment-postures.md#a-writable-console-has-two-preconditions).

**An `own_ops` console with no bound owner refuses every tenant read** with a 403
that names the remedy: set `POLYROB_OWNER_USER_ID` to the value the agent service
uses. Answering those reads would mean rendering an empty goal board and an empty
invoice list for an agent whose work lives under a different tenant.

**It will not write certain settings at any posture.** A flag that decides who the
agent obeys, what it may spend, whether a payment needs your approval, how much of
the host it can reach, or where its credentials live is writable only from the
local CLI (`polyrob config set`). The config screen marks those settings and the
refusal names the command to use instead. Preferences and ordinary flags write
normally; a guarded one asks for an explicit confirmation.

**A page outside its posture is absent, not denied.** Admin pages and sign-in exist
only in `multitenant`; elsewhere the route is not registered and the request 404s.
Inside `multitenant`, a signed-in non-admin following an admin link is redirected
home.

**A reader with nothing to read refuses rather than answering zero.** The files a
session produced are a session-scoped question, so asking for them without naming a
session is refused with that reason instead of an empty list; a store that would
not open is reported as unreadable with its reason. Anywhere you see a dash and a
reason, that is the console declining to state a number it did not measure.

---

## Naming

The console is called "POLYROB Console" regardless of what your instance is named.
Renaming the instance and branding the console are two separate, deliberate
choices; set `POLYROB_CONSOLE_NAME` to change the second one.

---

## See also

- [deployment-postures.md](deployment-postures.md) — postures, owner login, boot refusals
- [owner-controls.md](owner-controls.md) — pause, approvals, and the verbs, on every seat
- [payments.md](payments.md) — the wallet, invoicing and what the Money screens report
- [configuration.md](configuration.md) — how settings work; [../CONFIGURATION.md](../CONFIGURATION.md) is the full flag reference
- [api.md](api.md) — REST, A2A and the OpenAI-compatible surface
