# Migrating from Hermes Agent

What maps onto what, what has no equivalent, and the order to do it in.

*Hermes claims in this guide were checked against v0.19.0 (2026-07-20). It is a
fast-moving project — verify anything load-bearing against its repo.*

---

## Concept mapping

| Hermes | POLYROB | Notes |
|---|---|---|
| Provider / model | `DEFAULT_PROVIDER`, `DEFAULT_MODEL` | Set with `polyrob model set-default`; POLYROB holds keys for several providers at once and fails over between them |
| Profiles | **Profiles** (`polyrob -P <name>`) | A whole isolated home per identity: env, characters, skills, memory, goals, sessions. See [profiles.md](../profiles.md) |
| Gateway | `polyrob gateway`, or one process per surface | Same idea: every enabled chat surface in one process |
| Skills | Skills, plus agent-authored ones under `SKILLS_WRITABLE` | Different frontmatter shape — see below |
| H-MEM | `MEMORY_BACKEND` | SQLite FTS5 by default, optional local vector recall, tenant-scoped |
| Cron jobs | Cron **and** the goal board | Both durable; a goal carries acceptance criteria and dependencies, a cron job carries a schedule. See [streams.md](../streams.md) |
| Terminal backends | `AGENT_COMPUTE_POSTURE` 0–3 | Hardened Docker exec, then a persistent `shell` and `process` manager in a dev container, then gated self-maintenance verbs |
| Nous Portal | — | No single-subscription portal. One OpenRouter key reaches most models; `providers.yaml` declares any other endpoint |

---

## 1. Install and configure

```bash
pipx install "polyrob[all]"
polyrob init
python -m playwright install chromium   # only if you want browser automation
```

`polyrob init` writes `~/.polyrob/.env` and asks for a provider key. Everything
Hermes kept in `hermes.json` is an environment flag or a preference here:
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
> turns on the *interactive* tools only. The self-directed loops — goals, the
> planner, self-wake, writable skills — additionally need `AUTONOMY_ENABLED`.
> `polyrob autonomy on` sets it; `polyrob autonomy status` shows where you are.

---

## 2. Bring your skills

Both projects use `SKILL.md` with YAML frontmatter, but the frontmatter differs.
POLYROB follows the agentskills.io standard, so everything POLYROB-specific is
namespaced under `metadata` as flat `polyrob-*` string values — the valid key set
is in [skills.md](../skills.md#the-skillmd-format-quick-reference). Hermes'
top-level `triggers:` becomes `metadata.polyrob-triggers`, JSON-encoded as one
string:

```yaml
---
name: web-scraper
description: Scrapes web pages
license: MIT
metadata:
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["scrape"],"task_patterns":[],"tool_ids":[]}'
---
```

Copy `~/.hermes/skills/user/*` into your POLYROB skills root, then validate:

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

Operator-authored identity prose (Hermes' persona text) goes in the SOUL documents
under your data home — see [instances.md](../instances.md).

---

## 4. Memory

Hermes has no built-in memory export, so there is nothing to convert. Recreate what
matters by telling the agent, or — better for anything stable — write it as a skill
or ingest it into the knowledge base:

```bash
polyrob kb add ./notes
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
accounts — not that they are stubs.

POLYROB adds something Hermes' channel model does not have: **group rooms**. The
agent can answer anyone in a room you have allowed, from a read-only room toolset,
with per-room policy, roles and caps. See [groups.md](../groups.md).

---

## 6. Where POLYROB is behind

- **Nous Portal.** No single-subscription model catalog. One OpenRouter key gets you
  most of the reach.
- **Modal and Daytona backends.** POLYROB's compute ladder covers hardened Docker
  and a remote `ssh` backend. ⚠️ The `ssh` backend is **not sandboxed by default** —
  agent code runs with the SSH user's privileges — and a server refuses it unless you
  attest the host is disposable with `CODE_EXEC_SSH_SANDBOXED=true`.
- **iMessage and IRC.**
- **Companion mobile apps and voice modes.**

Agent-created skills are **not** on this list any more: POLYROB's learning loop ships
behind `SKILLS_WRITABLE`. Neither is provider failover or a durable task board — both
projects have those now.

---

## 7. Where POLYROB is ahead

Stated once, with detail in [comparison.md](../../comparison.md):

- **Economic agency** — wallet, x402 in and out, invoicing, on-chain trading,
  bridging, token deployment. Hermes has none. See [payments.md](../payments.md).
- **Four-tier access with origin taint** — owner, correspondent, group member,
  denied. Hermes' authorization is binary and single-operator. See
  [security-model.md](../security-model.md).
- **Proactive self-wake**, with depth and backoff guards and a no-change gate.
- **A durable owner-approval queue** you can answer from your phone. Hermes'
  approvals are in-memory and lost on restart.
- **A2A and a REST API**, plus an OpenAI-compatible surface. See [api.md](../api.md).
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
