# Migrating from OpenClaw to POLYROB

This guide helps you transition from OpenClaw to POLYROB, highlighting key differences and providing step-by-step migration instructions.

---

## Table of Contents

- [Concept Mapping](#concept-mapping)
- [Installation](#installation)
- [Configuration](#configuration)
- [Skills Migration](#skills-migration)
- [Memory Migration](#memory-migration)
- [Channel/Surface Differences](#channelsurface-differences)
- [CLI Differences](#cli-differences)
- [When to Stay with OpenClaw](#when-to-stay-with-openclaw)

---

## Concept Mapping

| OpenClaw Concept | POLYROB Equivalent | Notes |
|------------------|-------------------|-------|
| **Gateway** | `polyrob gateway` / Surfaces (Telegram, WhatsApp, Email) | Similar multi-platform support |
| **Channels** | Surfaces | POLYROB has fewer platforms currently |
| **Workspace** | Sessions | Similar concept, different structure |
| **Skills** | Skills system | Different frontmatter format (agentskills.io) |
| **SOUL.md** | SOUL/SELF identity docs (`/self` in chat) | Different storage path and frontmatter-free format |
| **DM pairing** | Correspondent access model | Similar security approach |
| **Sandbox** | Not supported | POLYROB uses least-privilege delegation (narrowed child toolset) instead of container/VM sandboxing |
| **Onboard** | `polyrob init` | Similar setup experience |

---

## Installation

### OpenClaw

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
```

### POLYROB

```bash
# Install with all features
pipx install "polyrob[all]"

# Initialize configuration
polyrob init

# Install browser (if using automation)
python -m playwright install chromium
```

**Key differences:**
- OpenClaw: Node.js/TypeScript, npm-based
- POLYROB: Python, pip/pipx-based
- OpenClaw: Daemon mode by default
- POLYROB: Foreground or systemd-managed

---

## Configuration

### OpenClaw Config Structure

```
~/.openclaw/
├── openclaw.json       # Main configuration
├── workspace/          # Session data and skills
│   ├── skills/         # User skills
│   ├── AGENTS.md       # Workspace instructions
│   └── SOUL.md         # Persona/identity
└── channels/           # Channel configs
```

### POLYROB Config Structure

```
~/.polyrob/
├── .env                # Environment variables (written by `polyrob init`)
└── cli.json            # Legacy (migrated to .env)

./.polyrob/                    # <data_home> for the local CLI (or $POLYROB_DATA_DIR)
├── .env                       # Project-local overrides
├── sessions/                  # Project sessions
├── skills/user_<uid>/         # Tenant-scoped skills you author/copy in
└── identity/
    ├── identity.md            # Operator-authored SOUL (frozen)
    ├── operating.md           # Operator-authored SOUL (frozen), optional
    └── rob/user_<uid>/self.md # Agent-writable SELF doc (rob = instance id)
```

### Configuration Translation

**OpenClaw (`openclaw.json`):**
```json
{
  "agent": {
    "model": "openrouter:gpt-4o"
  },
  "channels": {
    "telegram": {
      "token": "...",
      "dmPolicy": "pairing"
    }
  },
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "non-main"
      }
    }
  }
}
```

**POLYROB (`.env`):**
```bash
# Provider selection
DEFAULT_PROVIDER=openrouter
DEFAULT_MODEL=gpt-4o
OPENROUTER_API_KEY=sk-or-...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_SURFACE_ENABLED=true

# Access control (similar to dmPolicy)
CORRESPONDENT_ACCESS_ENABLED=true
CORRESPONDENT_REQUIRE_APPROVAL=true

# Local mode (enables safe features)
POLYROB_LOCAL=true
```

---

## Skills Migration

### OpenClaw Skills Location

```
~/.openclaw/workspace/skills/
└── my-skill/
    └── SKILL.md
```

### POLYROB Skills Location

POLYROB skills live under **`<data_home>/skills/`** — a writable root that survives a `polyrob update`
code-swap because it lives outside the installed package tree. `<data_home>` is the `POLYROB_DATA_DIR`
env var if you've set one (typical for a headless/server deployment), otherwise **`./.polyrob`**
relative to your current project directory for the local CLI.

```
<data_home>/skills/          # e.g. ./.polyrob/skills/ for a local project, by default
└── user_<uid>/              # Tenant-scoped, writable — copy/author your skills here
    └── my-skill/
        └── SKILL.md
```

There is no project-local `./skills/` read path today — a per-repo skill-discovery path is reserved
for a future release but not yet wired up for loading.

### Skill Format

OpenClaw and POLYROB both use Markdown files with YAML frontmatter, but the frontmatter shape differs.
POLYROB's skills use the **agentskills.io `SKILL.md` standard**: the only valid top-level keys are
`name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools` — an extra top-level
key is a hard validation error (`polyrob skills validate`). Anything POLYROB-specific (priority,
auto-activate, trigger keywords) is namespaced under `metadata` as flat `polyrob-*` **string** values
instead of living at the top level.

**OpenClaw SKILL.md:**
```markdown
---
name: task-automation
description: Automates repetitive tasks
triggers:
  - "automate"
version: "1.0.0"
---

# Task Automation

This skill automates...
```

**POLYROB SKILL.md:**

```markdown
---
name: task-automation
description: Automates repetitive tasks
license: MIT
metadata:
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["automate"],"task_patterns":[],"tool_ids":[]}'
---

# Task Automation

This skill automates...
```

There is **no top-level `triggers:`/`version:`/`auto_activate:`** in POLYROB's format — those live under
`metadata.polyrob-*` (`polyrob-triggers` is OpenClaw's `triggers:` list, JSON-encoded as one string).

### Migration Steps

1. **Copy skill files** into your project's data-home (swap in your own id; if you've set
   `POLYROB_DATA_DIR`, copy into that root instead of `./.polyrob`):
```bash
mkdir -p ./.polyrob/skills/user_<your-id>
cp -r ~/.openclaw/workspace/skills/* ./.polyrob/skills/user_<your-id>/
```

> **Prefer the managed path for skills you didn't author.** For a skill from a repo or URL, use
> `polyrob skill install <folder | owner/repo | git URL | SKILL.md URL>` instead of a manual copy —
> it threat-scans every file, quarantines for review (`polyrob skill approve`), and records an audit
> trail. See the **[Skills guide](../skills.md)** for the full flow and safety model.

2. **Fix frontmatter** on each copied skill to the agentskills.io shape above — `polyrob skills
   validate <skill-id>` reports what's missing.

3. **Migrate workspace instructions:**
```bash
# OpenClaw AGENTS.md → POLYROB project context file (auto-loaded under POLYROB_LOCAL)
cp ~/.openclaw/workspace/AGENTS.md ./AGENTS.md
```

4. **Migrate SOUL.md** — POLYROB's operator-authored identity doc is plain text (no frontmatter),
   read from `<data_home>/identity/identity.md`:
```bash
mkdir -p ./.polyrob/identity
cp ~/.openclaw/workspace/SOUL.md ./.polyrob/identity/identity.md
```

5. **Verify skills** (there's no manual force-load command — give the agent a task that should
   trigger the skill and it discovers it via the catalog and calls `load_skill` itself):
```bash
polyrob skills validate
polyrob skills list
polyrob chat
> [a task that should trigger task-automation]
```

---

## Memory Migration

### OpenClaw Memory

OpenClaw stores memories in the workspace with FTS5 search.

### POLYROB Memory

POLYROB uses SQLite FTS5 by default with optional vector search:

```bash
# Keyword search (default, like OpenClaw)
MEMORY_BACKEND=sqlite

# Semantic vector search
MEMORY_BACKEND=local_vector  # Requires pip install "polyrob[memory-vector]"
```

### Memory Export/Import

OpenClaw doesn't provide a built-in export. Recreate important memories:

```bash
polyrob chat
> I'm migrating from OpenClaw. Here are my key memories:
> 1. [memory 1]
> 2. [memory 2]
> 3. [memory 3]
>
> Please store these in memory.
```

Or create a knowledge-base skill for systematic migration.

---

## Channel/Surface Differences

### OpenClaw Channels

OpenClaw supports 20+ platforms through its gateway:

- WhatsApp, Telegram, Slack, Discord
- Google Chat, Signal, iMessage, IRC
- Microsoft Teams, Matrix, Feishu, LINE
- Mattermost, Nextcloud Talk, Nostr
- And more...

### POLYROB Surfaces

POLYROB currently supports:

- **CLI** — Terminal-native REPL
- **Telegram** — Via aiogram (local long-polling)
- **WhatsApp** — Cloud API webhook
- **Email** — IMAP/SMTP
- **REST API** — FastAPI server
- **Web Dashboard** — Local-first UI, single-user by default with an optional multitenant posture

`polyrob gateway` runs Telegram + WhatsApp + Email together in one process, the closest analog to
OpenClaw's gateway daemon; each surface can also be run standalone (`polyrob telegram`, `polyrob
whatsapp`, `polyrob email`).

### Setting Up Telegram

**OpenClaw:**
```bash
openclaw gateway setup
# Follow prompts for Telegram
openclaw gateway start
```

**POLYROB:**
```bash
# Configure in .env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_SURFACE_ENABLED=true

# Start the surface
polyrob telegram
```

### Setting Up Email

**OpenClaw:** Native support via gateway

**POLYROB:** v1 ships Gmail-flavored config (any IMAP/SMTP-compatible provider works if you point
the server fields at it):
```bash
# Configure in .env
EMAIL_SURFACE_ENABLED=true
GMAIL_EMAIL=you@example.com
GMAIL_APP_PASSWORD=...
# Optional — default to Gmail's IMAP/SMTP hosts, override for another provider:
# GMAIL_IMAP_SERVER=imap.gmail.com
# GMAIL_SMTP_SERVER=smtp.gmail.com
# GMAIL_SMTP_PORT=587

# Start the surface
polyrob email
```

---

## CLI Differences

### Command Comparison

| Task | OpenClaw | POLYROB |
|------|----------|---------|
| **Start chat** | `openclaw agent --message "..."` | `polyrob run "..."` |
| **Interactive** | Not typical | `polyrob chat` |
| **Change model** | Config file only | `polyrob model set-default` (interactive picker), or `polyrob model set-default <provider> <model>` after `polyrob model list` |
| **List skills** | Config file or UI | `/skills` in chat |
| **Gateway status** | `openclaw gateway status` | No status subcommand for `polyrob gateway` itself — `polyrob surface list` shows each surface's paused/active state |
| **Setup wizard** | `openclaw onboard` | `polyrob init` |
| **Health check** | `openclaw doctor` | `polyrob doctor` |

### Key Differences

1. **Architecture:**
   - OpenClaw: Gateway daemon + clients
   - POLYROB: Direct processes — `polyrob chat`/`polyrob run` for the CLI, `polyrob serve` for the API, `polyrob gateway` for a single multi-surface process, or one process per surface (`polyrob telegram`/`polyrob whatsapp`/`polyrob email`)

2. **Configuration:**
   - OpenClaw: JSON configuration file
   - POLYROB: Environment variables (.env)

3. **Language:**
   - OpenClaw: TypeScript/Node.js
   - POLYROB: Python

---

## Features Unique to Each

### OpenClaw Has, POLYROB Doesn't

- **Massive platform support** — 20+ messaging channels
- **Companion apps** — Windows Hub, macOS menu bar, iOS/Android
- **Live Canvas** — Agent-driven visual workspace
- **Voice modes** — Wake words, continuous voice
- **Sandboxing** — Docker/SSH/OpenShell execution backends
- **Node.js ecosystem** — For users who prefer JavaScript

### POLYROB Has, OpenClaw Doesn't

- **Multi-provider automatic failover** — Switch providers on errors
- **Durable goal board** — Goals survive restarts with CAS claims
- **Multi-tenant architecture** — Built for team/business use
- **Three-tier access model** — OWNER/CORRESPONDENT/DENIED
- **Capability gates** — Block high-impact tools for correspondents
- **A2A protocol** — Google's agent interoperability standard
- **REST API** — Built-in HTTP endpoints
- **Python ecosystem** — For users who prefer Python

---

## Migration Checklist

### Before Migration

- [ ] Identify critical OpenClaw skills
- [ ] Note your current model configuration
- [ ] Document important memories
- [ ] Check which channels you use
- [ ] Export SOUL.md and AGENTS.md

### Migration Steps

1. **Install POLYROB:**
   ```bash
   pipx install "polyrob[all]"
   python -m playwright install chromium
   ```

2. **Configure providers:**
   ```bash
   polyrob init
   # Edit ~/.polyrob/.env
   ```

3. **Migrate identity:**
   ```bash
   mkdir -p ./.polyrob/identity
   cp ~/.openclaw/workspace/SOUL.md ./.polyrob/identity/identity.md
   cp ~/.openclaw/workspace/AGENTS.md ./
   ```

4. **Migrate skills:**
   ```bash
   mkdir -p ./.polyrob/skills/user_<your-id>
   cp -r ~/.openclaw/workspace/skills/* ./.polyrob/skills/user_<your-id>/
   ```

5. **Set up surfaces:**
   ```bash
   # For Telegram
   polyrob telegram

   # For email
   polyrob email
   ```

6. **Test:**
   ```bash
   polyrob doctor
   polyrob run "test task"
   ```

### After Migration

- [ ] Verify all skills work
- [ ] Test surfaces (Telegram, Email)
- [ ] Recreate important memories
- [ ] Update any automation/scripts

---

## Example: Full Migration

```bash
# 1. Install POLYROB
pipx install "polyrob[all]"
python -m playwright install chromium

# 2. Export OpenClaw config notes
cat ~/.openclaw/openclaw.json > ~/openclaw-config-backup.json

# 3. Initialize POLYROB
polyrob init

# 4. Edit ~/.polyrob/.env with your keys
nano ~/.polyrob/.env

# 5. Migrate identity and workspace
mkdir -p ./.polyrob/identity
cp ~/.openclaw/workspace/SOUL.md ./.polyrob/identity/identity.md
cp ~/.openclaw/workspace/AGENTS.md ./

# 6. Migrate skills
mkdir -p ./.polyrob/skills/user_<your-id>
cp -r ~/.openclaw/workspace/skills/* ./.polyrob/skills/user_<your-id>/

# 7. Configure Telegram (if used)
# Add TELEGRAM_BOT_TOKEN to ~/.polyrob/.env
polyrob telegram

# 8. Test
polyrob doctor
polyrob chat

# 9. Verify skills — there's no manual force-load command; give the agent a task
# that should trigger the skill and it calls load_skill itself
polyrob skills validate
polyrob skills list
polyrob chat
> [a task that should trigger one of your migrated skills]

# 10. Clean up (optional)
# Once satisfied, you can remove OpenClaw
# openclaw gateway stop
# npm uninstall -g openclaw
```

---

## Security Model Comparison

### OpenClaw Security

- **DM pairing** — Unknown senders receive pairing code
- **Sandboxing** — Non-main sessions in Docker/SSH
- **Allowlists** — Per-channel allow lists
- **Config-driven** — Security settings in JSON

### POLYROB Security

- **Three-tier access** — OWNER/CORRESPONDENT/DENIED
- **Capability gates** — High-impact tools blocked for correspondents
- **Input sanitization** — Untrusted tool results wrapped
- **Tenant scoping** — Memory, goals, skills isolated by user_id
- **Env-driven** — Security settings via environment variables

### Translating Security Settings

**OpenClaw:**
```json
{
  "channels": {
    "telegram": {
      "dmPolicy": "pairing",
      "allowFrom": ["+1234567890"]
    }
  },
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "non-main"
      }
    }
  }
}
```

**POLYROB:**
```bash
# Enable correspondent access
CORRESPONDENT_ACCESS_ENABLED=true
CORRESPONDENT_REQUIRE_APPROVAL=true

# Specific user approval (via CLI) — ADDRESS is the sender's platform id
# (e.g. a numeric Telegram user id, not a phone number)
polyrob owner approve telegram 123456789

# Note: POLYROB doesn't have sandboxing yet
# Use least-privilege delegation instead
```

---

## When to Stay with OpenClaw

Consider staying with OpenClaw if:

- **You need omni-channel presence** — 20+ platforms is critical
- **You value companion apps** — Mobile/desktop apps are essential
- **You prefer Node.js** — Your stack is JavaScript-focused
- **You need sandboxing** — Isolated execution is required
- **You use Live Canvas** — Visual workspace is important

## When to Switch to POLYROB

Consider switching to POLYROB if:

- **You need provider redundancy** — Automatic failover is valuable
- **You run in production** — Multi-tenant architecture matters
- **You value security** — Access control and capability gates
- **You prefer Python** — Your stack is Python-focused
- **You need a REST API** — Programmatic access is required

---

## Getting Help

- **Documentation:** [README.md](../../../README.md)
- **Configuration:** [../../CONFIGURATION.md](../../CONFIGURATION.md)
- **Issues:** [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)
- **Comparison:** [../../comparison.md](../../comparison.md)

---

Still deciding? See the [feature comparison](../../comparison.md) for a detailed breakdown of POLYROB vs OpenClaw and other frameworks.
