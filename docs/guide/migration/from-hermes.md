# Migrating from Hermes Agent

What maps onto what, what has no equivalent, and the order to do it in.

*Hermes claims in this guide were checked on 2026-10-27 against v0.22.5
(2026-10-23) and its `main` documentation. It is a fast-moving project — verify
anything load-bearing against its repo.*

---

## Concept mapping

| Hermes | POLYROB | Notes |
|---|---|---|
| Provider / model | `DEFAULT_PROVIDER`, `DEFAULT_MODEL` | Set with `polyrob model set-default`; POLYROB holds keys for several providers at once and fails over between them |
| Profiles | **Profiles** (`polyrob -P <name>`) | A whole isolated home per identity: env, characters, skills, memory, goals, sessions. See [profiles.md](../profiles.md) |
| Gateway | `polyrob gateway`, or one process per chat app | Same idea: every enabled chat app in one process. Hermes now serves each of its `profile`s from one gateway; POLYROB runs one gateway (or systemd unit) per `profile` |
| Skills | Skills, plus agent-authored ones under `SKILLS_WRITABLE` | Different frontmatter shape — see below |
| Memory (`MEMORY.md`, `USER.md`, chat search) | `MEMORY_BACKEND` | SQLite FTS5 by default, optional local vector recall, scoped per user. See [§4](#4-memory) for bringing the two files over |
| `cron` jobs | Scheduled jobs | Durable scheduled runs. See [streams.md](../streams.md) |
| Kanban board, `/goal` | The goal board | A goal carries acceptance criteria and dependencies and survives a restart; Hermes' `/goal` lives in one chat |
| `/heartbeat`, `/loop` | Self-wake, or a scheduled job | Hermes wakes an idle chat on a timer; POLYROB's self-wake fires when observable state changes. Use a scheduled job for a fixed interval |
| Terminal backends | `AGENT_COMPUTE_POSTURE` 0–3, `SHELL_BACKEND` | Hardened Docker exec, then a persistent `shell` and `process` manager in a dev container, then gated self-maintenance verbs, then (posture 3) the host shell for your own terminal turn. Hermes' local backend is its default; here it is opt-in and refused with a signing key in the process |
| Terminal backends (remote) | `SHELL_BACKEND=ssh\|modal\|daytona\|vercel_sandbox\|singularity`, `CODE_EXEC_DOCKER_BINARY=podman` | ssh keeps one ControlMaster session; the cloud backends are optional packs (`polyrob[modal]`, …). Remote sandboxes do not sync your workspace |
| Dangerous-command approval (`approvals.mode`: `smart` / `manual` / `off`) | The shell command guard, `SHELL_APPROVAL_MODE` (`smart` / `every` / `per_command`), `SHELL_ALLOW`/`SHELL_DENY` | A floor no approval lifts (inside containers too); the dangerous class waits for you; unattended runs refuse it. `smart` may deny anything but approves only inside a sandbox |
| `process` (write/submit/close, pty, notify) | `process` + `shell_run(pty=, notify=)` | Same verbs; a notify note wakes the session, and receipts survive a restart |
| `execute_code` | `run_code(tools=True)`, `run_code(persist=True)` | Tool calls go through every gate; `CODE_EXEC_TOOL_CALLS` turns it on. `persist=True` keeps a per-session Python kernel |
| API server (`/v1/chat/completions`, `/v1/runs`) | The REST API, and `/v1` behind `OPENAI_COMPAT_API_ENABLED` | See [api.md](../api.md) |
| `A2A` plugin | Built-in `A2A` | The agent card, JSON-RPC and SSE. See [api.md](../api.md) |
| `hermes profile export` / `install` | `polyrob profile export` / `import` / `install` | Same idea; a Hermes archive does not import into POLYROB |
| Local models (`hermes model` → Local models) | `providers.yaml` | POLYROB does not install or size a llama.cpp server; point a row at the local server you run |
| Nous Portal (models and `Tool Gateway`) | — | No single-subscription portal. One OpenRouter key reaches most models; `providers.yaml` declares any other endpoint; web search and image abilities take their own keys |

---

## 1. Install and configure

```bash
pipx install "polyrob[all]"
polyrob init
python -m playwright install chromium   # only if you want browser automation
```

`polyrob init` writes `~/.polyrob/.env` and asks for a provider key. Everything
Hermes kept in `~/.hermes/config.yaml` and `~/.hermes/.env` is an environment flag
or a preference here:
[configuration.md](../configuration.md) explains how they resolve, and
[`docs/CONFIGURATION.md`](../../CONFIGURATION.md) lists every flag. A rough
translation:

```bash
DEFAULT_PROVIDER=openrouter
DEFAULT_MODEL=gpt-4o
OPENROUTER_API_KEY=sk-or-...
MEMORY_BACKEND=sqlite
```

Set more than one provider key and POLYROB fails over automatically on a
billing, quota or rate-limit error.

> **Autonomy is not on by default.** `POLYROB_LOCAL` (set for you by the CLI)
> turns on the *interactive* tools only. The self-directed loops — goals,
> self-wake, the curator, writable skills — additionally need `AUTONOMY_ENABLED`.
> `polyrob autonomy on` sets it; `polyrob autonomy status` shows where you are.

---

## 2. Bring your skills

Both projects use `SKILL.md` with YAML frontmatter, but the frontmatter differs.
Both follow the agentskills.io standard, and everything POLYROB-specific is
namespaced under `metadata` as flat `polyrob-*` string values — the valid key set
is in [skills.md](../skills.md#the-skillmd-format-quick-reference). Hermes'
top-level `version` and `platforms` keys and its `metadata.hermes` block (tags,
category, `requires_toolsets`, settings) have no meaning here; remove them. Rob sees
one line per skill and loads the skill whose line fits. Write that line as
`metadata.polyrob-when` (60 characters or fewer, it says when to load the skill) —
the "When to Use" section of a Hermes skill is a good source for it:

```yaml
---
name: web-scraper
description: Scrapes web pages
license: MIT
metadata:
  polyrob-when: 'When you need text or data from a web page'
---
```

Copy the skills you wrote from `~/.hermes/skills/` into your POLYROB skills root
(the bundled Hermes skills are copied there too — leave those), then validate:

```bash
polyrob skills validate          # names exactly what each skill is missing
polyrob skills list
```

For a skill you did **not** author, prefer `polyrob skill install <folder | owner/repo
| git URL | SKILL.md URL>` over a manual copy: it threat-scans every file, quarantines
for review, and records an audit trail. The skills root, the per-repo discovery of
`.agents/skills/` and `.claude/skills/`, and the whole safety model are in
[skills.md](../skills.md).

There is **no force-load command**. Start a chat and give the agent a task the skill
should serve; it finds the skill in its catalog and calls `load_skill` itself.

---

## 3. Bring your identity over

A Hermes profile and a POLYROB profile are the same idea — a separate bot with its
own everything. Two commands matter:

```bash
polyrob profile adopt mybot       # you already ran POLYROB in this folder: formalize it
polyrob profile import bot.tar.gz # you were handed a packaged identity
polyrob -P mybot                  # run as it
```

`adopt` lifts the identity documents, characters and identity-shaped env keys out of
a folder install into a real profile and pins the folder to it. Credentials never
travel inside an export. Full details, including running one systemd unit per
profile: [profiles.md](../profiles.md).

Operator-authored identity prose (Hermes' `SOUL.md`) goes in the SOUL documents
under your data home — see [instances.md](../instances.md).

---

## 4. Memory

Hermes keeps its built-in memory in two Markdown files, `~/.hermes/memories/MEMORY.md`
and `USER.md` (an external memory-provider plugin keeps its own store). POLYROB has
no importer for them. Read them, then recreate what matters by telling Rob,
or — better for anything stable — write it as a skill or ingest the files into the
knowledge base:

```bash
polyrob kb add ~/.hermes/memories
polyrob kb search "deployment approval"
```

POLYROB's own memory is on by default, cross-session and tenant-scoped.
`MEMORY_BACKEND=local_vector` adds hybrid vector recall (`pip install
"polyrob[memory-vector]"`); `/memory` in the REPL shows which provider is live.

---

## 5. Channels

`polyrob gateway` runs every enabled surface in one process — the closest analogue
to `hermes gateway start`. Each surface also runs standalone (`polyrob telegram`,
`polyrob email`, and so on); the full list is in [cli.md](../cli.md). An enabled
surface with missing credentials is warned about and skipped, so read the startup
output.

| Platform | Hermes | POLYROB |
|---|---|---|
| **CLI** | ✅ | ✅ |
| **Telegram** | ✅ | ✅ |
| **Email** | ✅ | ✅ |
| **WhatsApp** | ✅ | ✅ |
| **Discord** | ✅ | ✅ (built; live-account validation pending) |
| **Slack** | ✅ | ✅ (built; live-account validation pending) |
| **Signal** | ✅ | ✅ via signal-cli (built; live-account validation pending) |
| **X (DM)** | ❌ | ✅ (live-validated) |
| **iMessage** | ✅ | ❌ |
| **IRC** | ✅ | ❌ |

"Validation pending" means those surfaces are real clients against the real
endpoints, unit-tested with mocked transports, but not yet soak-tested against live
accounts — not that they are stubs. POLYROB also reaches Feishu / Lark and DingTalk,
which run under `polyrob gateway` — see
[feishu-dingtalk.md](../feishu-dingtalk.md).

Group chats work differently. Hermes gives each group its own admin list
and its own allowed slash commands; every other member still reaches the same
tools. A POLYROB **group room** answers its members from a read-only room toolset,
with per-room policy, roles and caps. See [groups.md](../groups.md).

---

## 6. Where POLYROB is behind

- **Nous Portal.** No single-subscription model catalog. One OpenRouter key gets you
  most of the reach.
- **Remote backends that sync your files.** The cloud backends (`modal`,
  `daytona`, `vercel_sandbox`) are optional packs and start without your files.
  ⚠️ The `ssh` backend is **not sandboxed by default** — agent code runs with the SSH
  user's privileges — and a server refuses it unless you attest the host is
  disposable with `CODE_EXEC_SSH_SANDBOXED=true`.
- **Chat app breadth** — 8 against ~29, including iMessage, IRC, Matrix and Teams.
- **The desktop app, voice chat and wake words.**
- **The plugin catalog** — hundreds of reviewed plugins against POLYROB's seven
  first-party packs.
- **One-click local models** — POLYROB reaches a local server you run yourself.
- **ACP for editors, and an importer** — `hermes import-agent` has no POLYROB
  counterpart; this guide is the migration.

Agent-created skills are **not** on this list any more: POLYROB's learning loop ships
behind `SKILLS_WRITABLE`. Neither is provider failover or a durable task board — both
projects have those now — nor `A2A`, an OpenAI-compatible API or portable `profile`s,
which Hermes now ships as well.

---

## 7. Where POLYROB is ahead

Stated once, with detail in [comparison.md](../../comparison.md):

- **Economic agency** — wallet, x402 in and out, invoicing, on-chain trading,
  bridging, token deployment, all under core spend limits. Hermes has only optional
  payment skills, read-only network skills and community wallet plugins. See
  [payments.md](../payments.md).
- **Four-tier access with origin taint** — owner, correspondent, group member,
  denied. Hermes' authorization is binary and single-operator. See
  [security-model.md](../security-model.md).
- **Per-user isolation** — several users in one process, each with their own memory,
  goals and chats. Hermes keeps one agent per home.
- **Change-driven self-wake**, with depth and backoff guards and a no-change gate.
  Hermes' `/heartbeat` and `/loop` wake on a timer.
- **A durable owner-approval queue** you can answer from your phone. Hermes'
  approvals wait in process with a timeout and are lost on restart.
- **Work that outlives the session** — an app the agent builds, kept alive behind a
  public URL by an owner-run supervisor.

---

## 8. Do it in this order

1. `pipx install "polyrob[all]"` and `polyrob init`.
2. Add your provider keys, then `polyrob doctor`.
3. Copy your skills, `polyrob skills validate`, fix the frontmatter.
4. Create a profile if you run more than one bot.
5. Start one surface you use daily and live on it for a week.
6. Re-create the memories and scheduled jobs that turned out to matter — not the
   ones you assumed would.
7. Turn on autonomy last: `polyrob autonomy on`, then watch
   `polyrob autonomy status`.

Keep Hermes running until step 5 has gone a full week.

---

## Help

- [`docs/CONFIGURATION.md`](../../CONFIGURATION.md) — every flag.
- [cli.md](../cli.md) — the command reference.
- [comparison.md](../../comparison.md) — feature-by-feature.
- [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues) ·
  [Discussions](https://github.com/theselfruleorg/polyrob/discussions)
