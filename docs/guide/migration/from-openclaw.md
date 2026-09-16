# Migrating from OpenClaw

What maps onto what, what has no equivalent, and the order to do it in.

*OpenClaw claims in this guide were checked against `main` on 2026-07-23. It is a
fast-moving project — verify anything load-bearing against its repo.*

---

## Concept mapping

| OpenClaw | POLYROB | Notes |
|---|---|---|
| Gateway daemon | `polyrob gateway`, or one process per surface | Every enabled chat surface in one process |
| Channels | Surfaces | Seven today; the list and the per-surface commands are in [cli.md](../cli.md) |
| Channel groups | **Rooms** | Allow-listed group chats with a read-only room toolset, roles, per-room policy and caps. See [groups.md](../groups.md) |
| Workspace | Sessions + a per-repo context file | Each session has its own workspace; `AGENTS.md` in your project is auto-loaded |
| `SOUL.md` | SOUL identity documents | Operator-authored, frozen, plain text with no frontmatter. See [instances.md](../instances.md) |
| Skills | Skills | agentskills.io frontmatter — see below |
| DM pairing | Pairing **and** the correspondent model | Pairing grants a sender owner-tier access; a correspondent's replies arrive as data. See [security-model.md](../security-model.md) |
| Sandbox modes | `CODE_EXEC_BACKEND` + `AGENT_COMPUTE_POSTURE` | Hardened Docker, a local subprocess (not a sandbox) or a remote host; code execution is opt-in |
| `openclaw onboard` | `polyrob init` | Same job |
| Node daemon | Foreground process or systemd unit | Python, pipx-installed |

---

## 1. Install and configure

```bash
pipx install "polyrob[all]"
polyrob init
python -m playwright install chromium   # only if you want browser automation
```

OpenClaw keeps its configuration in `openclaw.json`; POLYROB uses environment flags
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
CORRESPONDENT_ACCESS_ENABLED=true       # a party the agent contacted may reply, as data
CORRESPONDENT_REQUIRE_APPROVAL=true
```

Set more than one provider key and POLYROB fails over automatically on a billing,
quota or rate-limit error. If you used OpenClaw's ChatGPT/Codex sign-in, POLYROB has
the same idea: `polyrob config set LLM_OAUTH_ENABLED true`, then
`polyrob auth add openai-codex` — read the terms-of-service warning it prints first.

> **Autonomy is not on by default.** `POLYROB_LOCAL` (set for you by the CLI) turns
> on the *interactive* tools only. The self-directed loops — goals, the planner,
> self-wake, writable skills — additionally need `AUTONOMY_ENABLED`.
> `polyrob autonomy on` sets it; `polyrob autonomy status` shows where you are.

---

## 2. Bring your skills

Both projects use `SKILL.md` with YAML frontmatter, but the frontmatter differs.
POLYROB follows the agentskills.io standard: the only valid top-level keys are
`name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`,
and everything POLYROB-specific is namespaced under `metadata` as flat `polyrob-*`
string values. OpenClaw's top-level `triggers:` becomes `metadata.polyrob-triggers`,
JSON-encoded as one string:

```yaml
---
name: task-automation
description: Automates repetitive tasks
license: MIT
metadata:
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["automate"],"task_patterns":[],"tool_ids":[]}'
---
```

Copy `~/.openclaw/workspace/skills/*` into your POLYROB skills root, then validate:

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

OpenClaw has no built-in memory export, so there is nothing to convert. Recreate
what matters by telling the agent, or — better for anything stable — ingest it:

```bash
polyrob kb add ./notes
polyrob kb search "deployment approval"
```

POLYROB's own memory is on by default, cross-session and tenant-scoped.
`MEMORY_BACKEND=local_vector` adds hybrid vector recall (`pip install
"polyrob[memory-vector]"`); `/memory` in the REPL shows which provider is live.

---

## 5. Channels

This is where you give something up. OpenClaw reaches ~23 platforms; POLYROB has
seven — Telegram, WhatsApp, Email, Discord, Slack, Signal and X — plus the CLI, the
REST API and the console. iMessage, Matrix, Feishu, LINE, Teams and the rest have no
equivalent.

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

- **Channel breadth** — seven against ~23.
- **Companion apps** — no Windows Hub, macOS menu bar or mobile nodes.
- **Live Canvas** — no agent-driven visual workspace.
- **Voice modes** — no wake words, no continuous voice.
- **Sandboxing on by default** — POLYROB has a hardened Docker backend and a remote
  `ssh` one, but code execution is opt-in (`CODE_EXEC_ENABLED`, default off) rather
  than on for non-main sessions. ⚠️ The `local_subprocess` backend is a convenience,
  **not a sandbox**; a server refuses it.
- **The Node.js ecosystem**, if that is your stack.

---

## 7. Where POLYROB is ahead

Stated once, with detail in [comparison.md](../../comparison.md):

- **Economic agency** — wallet, x402 in and out, invoicing, on-chain trading,
  bridging, token deployment. OpenClaw has none. See [payments.md](../payments.md).
- **Four-tier access with origin taint** — owner, correspondent, group member,
  denied, with high-impact tools gated off while a session is correspondent-tainted.
  OpenClaw's pairing gates *channel access*, not individual actions. See
  [security-model.md](../security-model.md).
- **A durable goal board** with objectives, dependencies and restart-safe claims,
  plus **proactive self-wake**. See [streams.md](../streams.md).
- **A durable owner-approval queue** you can answer from your phone.
- **MCP server as well as client** — expose POLYROB's own tools to Claude Desktop or
  Cursor. Plus A2A and a REST/OpenAI-compatible API. See [api.md](../api.md).
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
