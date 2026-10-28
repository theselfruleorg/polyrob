# Migrating from OpenClaw

What maps onto what, what has no equivalent, and the order to do it in.

*OpenClaw claims in this guide were checked on 2026-10-27 against release
2026.10.8 and its `main` documentation. It is a fast-moving project — verify
anything load-bearing against its repo.*

---

## Concept mapping

| OpenClaw | POLYROB | Notes |
|---|---|---|
| Gateway daemon | `polyrob gateway`, or one process per surface | Every enabled chat surface in one process |
| Channels | Surfaces | Nine today; the list and the per-surface commands are in [cli.md](../cli.md) |
| Channel groups | **Rooms** | Allow-listed group chats with a read-only room toolset, roles, per-room policy and caps. See [groups.md](../groups.md) |
| Workspace | Sessions + a per-repo context file | Each session has its own workspace; `AGENTS.md` in your project is auto-loaded |
| `SOUL.md` | SOUL identity documents | Operator-authored, frozen, plain text with no frontmatter. See [instances.md](../instances.md) |
| Skills | Skills | agentskills.io frontmatter — see below |
| DM pairing | Pairing **and** the correspondent model | Pairing grants a sender owner-tier access; a correspondent's replies arrive as data. See [security-model.md](../security-model.md) |
| Sandbox settings (`agents.defaults.sandbox`) | `CODE_EXEC_BACKEND` + `AGENT_COMPUTE_POSTURE` | Hardened Docker, a local subprocess (not a sandbox) or a remote host; code execution is opt-in. Both projects leave sandboxing off by default |
| Exec approvals | The shell command guard + the owner-approval queue | A POLYROB approval waits in a durable queue and survives a restart; an OpenClaw approval that waits ends with its turn |
| Memory files (`MEMORY.md`, `USER.md`, `memory/*.md`) | `MEMORY_BACKEND` + the knowledge base | See [§4](#4-memory) |
| Automations (`openclaw automations`, alias `cron`) | Scheduled jobs | Durable scheduled runs. See [streams.md](../streams.md) |
| Chat goals (`/goal`), Workboard plugin | The goal board | A goal carries acceptance criteria and dependencies and survives a restart |
| Multi-user setup, operator roles | Users + the access tiers | OpenClaw shares one trust domain per Gateway; POLYROB scopes each user by `user_id`. See [security-model.md](../security-model.md) |
| `A2A` channel plugin, `openclaw mcp serve` | Built-in `A2A`, `POST /mcp` | See [api.md](../api.md) |
| `openclaw onboard` | `polyrob init` | Same job |
| Node daemon | Foreground process or systemd unit | Python, pipx-installed |

---

## 1. Install and configure

```bash
pipx install "polyrob[all]"
polyrob init
python -m playwright install chromium   # only if you want browser automation
```

OpenClaw keeps its configuration in `~/.openclaw/openclaw.json`; POLYROB uses environment settings
and live preferences. [configuration.md](../configuration.md) explains how they
resolve and which command writes which; [`docs/CONFIGURATION.md`](../../CONFIGURATION.md)
lists every flag. A rough translation of a typical `openclaw.json`:

```bash
DEFAULT_PROVIDER=openrouter
DEFAULT_MODEL=gpt-4o
OPENROUTER_API_KEY=sk-or-...

TELEGRAM_BOT_TOKEN=...
TELEGRAM_SURFACE_ENABLED=true

# dmPolicy: "pairing" has two halves here
POLYROB_REQUIRE_PAIRING=true            # a stranger must be approved to steer
# a party the agent contacted may reply, as data (a posture member: it rides POLYROB_OVERRIDES)
POLYROB_OVERRIDES=CORRESPONDENT_ACCESS_ENABLED=true
CORRESPONDENT_REQUIRE_APPROVAL=true
```

Set more than one provider key and POLYROB fails over automatically on a billing,
quota or rate-limit error. If you used OpenClaw's ChatGPT/Codex sign-in, POLYROB has
the same idea: `polyrob config set LLM_OAUTH_ENABLED true`, then
`polyrob auth add openai-codex` — read the terms-of-service warning it prints first.

> **Autonomy is not on by default.** `POLYROB_LOCAL` (set for you by the CLI) turns
> on the *interactive* tools only. The self-directed loops — goals, self-wake,
> the curator, writable skills — additionally need `AUTONOMY_ENABLED`.
> `polyrob autonomy on` sets it; `polyrob autonomy status` shows where you are.

---

## 2. Bring your skills

Both projects use `SKILL.md` with YAML frontmatter, but the frontmatter differs.
POLYROB follows the agentskills.io standard: the only valid top-level keys are
`name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`,
and everything POLYROB-specific is namespaced under `metadata` as flat `polyrob-*`
string values. OpenClaw also follows AgentSkills, but its own keys — `homepage`,
`user-invocable`, `disable-model-invocation`, `command-dispatch` and the
`metadata.openclaw` gating block — have no meaning here; remove them. Rob sees one
line per skill and loads the skill whose line fits. Write that line as
`metadata.polyrob-when` (60 characters or fewer, it says when to load the skill):

```yaml
---
name: task-automation
description: Automates repetitive tasks
license: MIT
metadata:
  polyrob-when: 'When a task repeats the same steps each time'
---
```

Copy your skills from `~/.openclaw/workspace/skills/` and the managed
`~/.openclaw/skills/` into your POLYROB skills root, then validate:

```bash
polyrob skills validate          # names exactly what each skill is missing
polyrob skills list
```

For a skill you did **not** author, prefer `polyrob skill install <folder | owner/repo
| git URL | SKILL.md URL>` over a manual copy: it threat-scans every file, quarantines
for review, and records an audit trail. The skills root, per-repo discovery of
`.agents/skills/` and `.claude/skills/`, and the safety model are in
[skills.md](../skills.md).

There is **no force-load command**. Give the agent a task the skill should serve; it
finds the skill in its catalog and calls `load_skill` itself.

---

## 3. Bring your identity over

OpenClaw's `SOUL.md` and `AGENTS.md` have direct homes:

- **`AGENTS.md`** — copy it to your project root. POLYROB auto-loads one per-repo
  context file under `POLYROB_LOCAL`, and `AGENTS.md` is one of the names it
  recognises. Use `polyrob.md` instead if you want to steer POLYROB without touching
  the file your other coding agents read.
- **`SOUL.md`** — the operator-authored identity document, plain text, no
  frontmatter. `polyrob identity soul` manages it; see
  [instances.md](../instances.md).

If you ran more than one OpenClaw bot, use **profiles** — a whole isolated home per
identity, with its own env, characters, skills, memory, goals and sessions:

```bash
polyrob profile adopt mybot       # you already ran POLYROB in this folder: formalize it
polyrob profile import bot.tar.gz # you were handed a packaged identity
polyrob -P mybot                  # run as it
```

Credentials never travel inside an export. Full details, including one systemd unit
per profile: [profiles.md](../profiles.md).

---

## 4. Memory

OpenClaw's memory is plain Markdown in its `workspace`: `MEMORY.md`, `USER.md` and
the dated notes under `memory/`. POLYROB has no importer for them. Read them, then
recreate what matters by telling Rob, or — better for anything stable —
ingest the notes into the knowledge base:

```bash
polyrob kb add ~/.openclaw/workspace/memory
polyrob kb search "deployment approval"
```

POLYROB's own memory is on by default, cross-session and tenant-scoped.
`MEMORY_BACKEND=local_vector` adds hybrid vector recall (`pip install
"polyrob[memory-vector]"`); `/memory` in the REPL shows which provider is live.

---

## 5. Channels

This is where you give something up. OpenClaw reaches ~33 platforms; POLYROB has
nine — Telegram, WhatsApp, Email, Discord, Slack, Signal, Feishu / Lark, DingTalk and
X — plus the CLI, the REST API and the console. iMessage, Matrix, LINE, Teams,
Nostr, Twitch and the rest have no equivalent. Feishu / Lark and DingTalk:
[feishu-dingtalk.md](../feishu-dingtalk.md).

`polyrob gateway` runs every enabled surface in one process, which is the closest
analogue to the OpenClaw gateway daemon; each also runs standalone. The commands are
in [cli.md](../cli.md). An enabled surface with missing credentials is warned about
and skipped, so read the startup output.

Two things work differently rather than being fewer:

- **Email gives the agent its own address.** Set `AGENTMAIL_API_KEY` and it
  provisions a managed inbox on first run — no IMAP or SMTP setup. The legacy path
  (`GMAIL_EMAIL` + `GMAIL_APP_PASSWORD`, any IMAP/SMTP provider) still works; pick
  one with `EMAIL_PROVIDER`.
- **Group chats are a regime, not a channel.** A room you allow gets a read-only
  room toolset, per-chat roles, a per-chat policy (`chat.mode`, wake words, reply
  caps, quiet hours) and an optional recurring catch-up job. Nothing is answered in
  a room you have not allowed. See [groups.md](../groups.md).

---

## 6. Where POLYROB is behind

- **Chat app breadth** — nine against ~33.
- **Companion apps** — no Windows Hub, macOS menu bar or mobile nodes.
- **Live Canvas** — no agent-driven visual workspace.
- **Voice modes** — no wake words, no continuous voice.
- **Team features in the UI** — no shared chats with owners, presence and
  mentions like OpenClaw's Control UI.
- **ACP for editors, and an importer** — `openclaw migrate` has no POLYROB
  counterpart; this guide is the migration.
- **The Node.js ecosystem**, if that is your stack.

⚠️ Code execution is opt-in here (`CODE_EXEC_ENABLED`, default off), as OpenClaw's
sandboxing is. The `local_subprocess` backend is a convenience, **not a sandbox**;
a server refuses it.

---

## 7. Where POLYROB is ahead

Stated once, with detail in [comparison.md](../../comparison.md):

- **Economic agency** — wallet, x402 in and out, invoicing, on-chain trading,
  bridging, token deployment. OpenClaw has none. See [payments.md](../payments.md).
- **Four-tier access with origin taint** — owner, correspondent, group member,
  denied, with high-impact tools gated off while a session is correspondent-tainted.
  OpenClaw's pairing, owner-only abilities and operator roles all sit inside one trust
  domain. See [security-model.md](../security-model.md).
- **Per-user isolation** — several users in one process, each with their own memory,
  goals and chats. OpenClaw tells you to run separate Gateways instead.
- **A durable goal board** with dependencies and restart-safe claims, scheduled jobs,
  plus **change-driven self-wake**. OpenClaw's Workboard is an opt-in plugin. See
  [streams.md](../streams.md).
- **A durable owner-approval queue** you can answer from your phone, and that
  survives a restart.
- **Work that outlives the session** — an app the agent builds, kept alive behind a
  public URL by an owner-run supervisor.

---

## 8. Do it in this order

1. `pipx install "polyrob[all]"` and `polyrob init`.
2. Add your provider keys, then `polyrob doctor`.
3. Copy `AGENTS.md` and `SOUL.md`; copy your skills and run `polyrob skills validate`.
4. Create a profile if you run more than one bot.
5. Bring up the one channel you use most and live on it for a week.
6. Allow your busiest group chat with `/groups allow here` and set its `chat.mode`.
7. Re-create the memories and jobs that turned out to matter — not the ones you
   assumed would.
8. Turn on autonomy last: `polyrob autonomy on`, then watch `polyrob autonomy status`.

Keep OpenClaw running until step 5 has gone a full week.

---

## Help

- [`docs/CONFIGURATION.md`](../../CONFIGURATION.md) — every flag.
- [cli.md](../cli.md) — the command reference.
- [comparison.md](../../comparison.md) — feature-by-feature.
- [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues) ·
  [Discussions](https://github.com/theselfruleorg/polyrob/discussions)
