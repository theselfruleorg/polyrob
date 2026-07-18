# Migrating from Hermes Agent to POLYROB

This guide helps you transition from Hermes Agent to POLYROB, highlighting key differences and providing step-by-step migration instructions.

---

## Table of Contents

- [Concept Mapping](#concept-mapping)
- [Installation](#installation)
- [Configuration](#configuration)
- [Skills Migration](#skills-migration)
- [Memory Migration](#memory-migration)
- [CLI Differences](#cli-differences)
- [Gateway vs Surfaces](#gateway-vs-surfaces)
- [When to Stay with Hermes](#when-to-stay-with-hermes)

---

## Concept Mapping

| Hermes Concept | POLYROB Equivalent | Notes |
|----------------|-------------------|-------|
| **Provider/Model** | `DEFAULT_PROVIDER`, `DEFAULT_MODEL` | Same configuration via env |
| **Profiles** | Not directly supported | POLYROB uses tenant isolation instead |
| **Gateway** | `polyrob gateway` / Surfaces (Telegram, WhatsApp, Email, Discord, Slack, Signal, X) | Similar architecture |
| **Skills** | Skills system (incl. agent-authored via `SKILLS_WRITABLE`) | Similar but different storage |
| **H-MEM** | Memory backend | POLYROB uses SQLite FTS5 by default |
| **Cron jobs** | Cron + Goal board | More durable in POLYROB |
| **Terminal backends** | Compute-posture ladder (`AGENT_COMPUTE_POSTURE`) | Sandboxed code exec → persistent `shell`/`process` in a dev container → self-maintain verbs; Docker-backed. No SSH/Modal/Daytona remotes |
| **Nous Portal** | Not supported | POLYROB is multi-provider by design (OpenRouter gets you one-key access) |

---

## Installation

### Hermes

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup
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

**Key difference:** POLYROB uses pip/pipx instead of a custom installer, giving you more control over the installation environment.

---

## Configuration

### Hermes Config Structure

```
~/.hermes/
├── hermes.json        # Main configuration
├── profiles/          # Per-profile settings
├── skills/            # User skills
└── workspace/         # Session data
```

### POLYROB Config Structure

```
~/.polyrob/
├── .env               # Environment variables
├── cli.json           # Legacy (migrated to .env)
└── sessions/          # Project sessions (per-project)

./.polyrob/
├── .env               # Project-local overrides
└── sessions/          # Project sessions
```

### Configuration Translation

**Hermes (`hermes.json`):**
```json
{
  "agent": {
    "model": "openrouter:gpt-4o"
  },
  "integrations": {
    "providers": {
      "openrouter": {
        "apiKey": "sk-or-..."
      }
    }
  },
  "memory": {
    "backend": "fts5"
  }
}
```

**POLYROB (`.env`):**
```bash
# Provider selection
DEFAULT_PROVIDER=openrouter
DEFAULT_MODEL=gpt-4o

# API keys
OPENROUTER_API_KEY=sk-or-...

# Memory backend
MEMORY_BACKEND=sqlite

# Optional features
POLYROB_LOCAL=true    # Enable safe autonomy
SKILLS_WRITABLE=true  # Allow skill creation
GOALS_ENABLED=true    # Enable goal board
```

### Provider Configuration

**Hermes:** Configure via `integrations.providers` in hermes.json

**POLYROB:** Set provider-specific API keys in `.env`:

```bash
# POLYROB supports multiple providers simultaneously
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
DEEPSEEK_API_KEY=...
OPENROUTER_API_KEY=...
NVIDIA_API_KEY=...
```

POLYROB will automatically fail over to another provider if the primary encounters billing or rate-limit errors.

---

## Skills Migration

### Hermes Skills Location

```
~/.hermes/skills/
├── user/
│   ├── my-skill/
│   │   └── SKILL.md
└── managed/
    └── ...
```

### POLYROB Skills Location

POLYROB skills live under **`<data_home>/skills/`** — a writable root that survives a `polyrob update`
code-swap because it lives outside the installed package tree (`polyrob update` snapshots it before every
update, alongside `identity/`). `<data_home>` resolves the same way everywhere in POLYROB
(`core/runtime_paths.py`/`core/bootstrap.py::_resolve_cli_data_home`): the `POLYROB_DATA_DIR` env var if
you've set one (typical for a headless/server deployment), otherwise **`./.polyrob`** relative to your
current project directory for the local CLI (`polyrob chat`/`polyrob run`) — there is no single global
`~/.polyrob/skills/` unless you've pointed `POLYROB_DATA_DIR` at your home directory.

```
<data_home>/skills/          # e.g. ./.polyrob/skills/ for a local project, by default
└── user_<uid>/              # Tenant-scoped, writable — copy/author your skills here
    └── my-skill/
        └── SKILL.md
```

The skills bundled with POLYROB itself ship separately, read-only, in the installed package's
`data/prompts/skills/` — that's not where you copy your own skills.

> A per-repo skill-discovery path (`.agents/skills/`-style, no copying needed) is reserved in the
> storage-scope precedence order but not yet wired up for loading — a plain project-local `./skills/`
> is not read today.

### Skill Format

Hermes and POLYROB both use Markdown files with YAML frontmatter, but the frontmatter shape differs.
POLYROB's skills use the **agentskills.io `SKILL.md` standard**: the only valid top-level keys are
`name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools` — an extra top-level
key is a hard validation error (`polyrob skills validate`). Anything POLYROB-specific (priority,
auto-activate, trigger keywords, schema version) is namespaced under `metadata` as flat `polyrob-*`
**string** values instead of living at the top level.

**Hermes SKILL.md:**
```markdown
---
name: web-scraper
description: Scrapes web pages
triggers:
  - "scrape"
version: "1.0.0"
---

# Web Scraper

This skill scrapes web pages...
```

**POLYROB SKILL.md:**

```markdown
---
name: web-scraper
description: Scrapes web pages
license: MIT
metadata:
  polyrob-priority: '5'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["scrape"],"task_patterns":[],"tool_ids":[]}'
  polyrob-version: '1'
---

# Web Scraper

This skill scrapes web pages...
```

There is **no top-level `triggers:`/`version:`/`auto_activate:`** in POLYROB's format — those live under
`metadata.polyrob-*` (`polyrob-triggers` is Hermes' `triggers:` list, JSON-encoded as one string).

### Migration Steps

1. **Copy skill files** into your project's data-home (swap in your own id; if you've set
   `POLYROB_DATA_DIR`, copy into that root instead of `./.polyrob`):
```bash
mkdir -p ./.polyrob/skills/user_<your-id>
cp -r ~/.hermes/skills/user/* ./.polyrob/skills/user_<your-id>/

# Or, for a headless/shared deployment with POLYROB_DATA_DIR set:
mkdir -p "$POLYROB_DATA_DIR/skills/user_<your-id>"
cp -r ~/.hermes/skills/user/* "$POLYROB_DATA_DIR/skills/user_<your-id>/"
```

> **Prefer the managed path for skills you didn't author.** For a skill from a repo or URL, use
> `polyrob skill install <folder | owner/repo | git URL | SKILL.md URL>` instead of a manual copy —
> it threat-scans every file, quarantines for review (`polyrob skill approve`), and records an audit
> trail. See the **[Skills guide](../skills.md)** for the full flow and safety model.

2. **Add/fix frontmatter** on each copied skill to the agentskills.io shape above if it doesn't already
   have `name`/`description`/`license` — `polyrob skills validate <skill-id>` reports what's missing.

3. **Verify compatibility** (outside the chat REPL):
```bash
polyrob skills validate
polyrob skills list
```

4. **Test each skill** — there's no manual force-load command; start a chat and give it a task that
   should trigger the skill, and the agent discovers it via the skill catalog and calls `load_skill`
   itself:
```bash
polyrob chat
> [a task that should trigger web-scraper, e.g. "scrape the pricing page at <url>"]
```

---

## Memory Migration

### Hermes H-MEM

Hermes uses FTS5 with optional embeddings:

```json
{
  "memory": {
    "backend": "fts5"  // or "embeddings"
  }
}
```

### POLYROB Memory

POLYROB uses SQLite FTS5 by default, with optional vector search:

```bash
# Keyword search (default)
MEMORY_BACKEND=sqlite

# Semantic vector search
MEMORY_BACKEND=local_vector  # Requires pip install "polyrob[memory-vector]"
```

### Memory Export/Import

**Hermes doesn't provide a built-in export.** You'll need to manually recreate important memories:

```bash
polyrob chat
> In Hermes, I had these memories:
> 1. Project X uses PostgreSQL 14 on AWS RDS
> 2. API key for Service Y is stored in vault at /production/api-keys/service-y
> 3. Our deployment process requires approval from @tech-lead
>
> Please store these in memory.
```

For systematic migration, consider creating a skill:

**SKILL.md** (POLYROB's agentskills.io shape — see [Skill Format](#skill-format) above):
```markdown
---
name: project-context
description: Project X context and deployment
license: MIT
metadata:
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["project x"],"task_patterns":[],"tool_ids":[]}'
---

# Project X Context

## Infrastructure
- Database: PostgreSQL 14 on AWS RDS
- Instance: db.project-x.production
- Backup: Daily snapshots, 30-day retention

## API Keys
- Service Y: Stored in vault at `/production/api-keys/service-y`
- Rotation: Monthly, coordinated with DevOps

## Deployment
- Requires approval from @tech-lead
- Deploy window: Tuesday-Thursday, 9am-11am PST
- Rollback procedure: Documented in runbooks/deploy-project-x.md
```

---

## CLI Differences

### Command Comparison

| Task | Hermes | POLYROB |
|------|--------|---------|
| **Start chat** | `hermes` | `polyrob chat` |
| **Run one-shot** | `hermes agent --message "..."` | `polyrob run "..."` |
| **Change model** | `hermes model <provider:model>` | `polyrob model set-default <provider> <model>` |
| **List skills** | `/skills` in chat | `/skills` in chat |
| **Load skill** | `/<skill-name>` | none — the agent discovers a skill from the catalog and calls `load_skill` itself when a task matches it |
| **Compress context** | `/compress` | `/compress` |
| **New session** | `/new` | `/clear` resets context in the current session; exit and run `polyrob chat` (or `polyrob run "..."`) for a genuinely new session id |
| **Configuration** | `hermes config` | `polyrob config show` |
| **Doctor/health** | `hermes doctor` | `polyrob doctor` |

### Key Differences

1. **Model selection syntax:**
   - Hermes: `hermes model openrouter:gpt-4o`
   - POLYROB: `polyrob model set-default` with no arguments opens an interactive picker; or run `polyrob model list` to see what's available, then `polyrob model set-default <provider> <model>`

2. **Gateway vs API server:**
   - Hermes: `hermes gateway`
   - POLYROB: `polyrob gateway` runs every enabled surface (Telegram, WhatsApp, Email,
     Discord, Slack, Signal, X) in one process, or `polyrob serve` for the REST API
     alone. An enabled surface with missing credentials is warned about and skipped —
     check the startup output

3. **Terminal backends:**
   - Hermes supports Docker, SSH, Modal, Daytona
   - POLYROB ships a compute-posture ladder (`AGENT_COMPUTE_POSTURE` 0–3): hardened
     Docker code-exec by default, a persistent `shell` + `process` job manager in a
     per-session dev container at posture ≥1, and gated self-maintenance verbs at ≥2.
     Remote execution backends (SSH/Modal/Daytona) are not supported

---

## Gateway vs Surfaces

### Hermes Gateway

Hermes uses a single gateway process for all messaging platforms:

```bash
hermes gateway setup    # Configure platforms
hermes gateway start    # Start gateway
```

### POLYROB Surfaces

`polyrob gateway` is the closest direct equivalent to `hermes gateway` — it runs every
enabled surface in one process. POLYROB also lets you run each surface as its own
process if you'd rather keep them separate:

```bash
# All enabled surfaces in one process (closest to `hermes gateway start`)
polyrob gateway

# Or run surfaces individually:

# Telegram (if installed with the [telegram] extra)
polyrob telegram

# WhatsApp Cloud API (webhook server)
polyrob whatsapp

# Email — IMAP poll + SMTP, no extra install needed (stdlib imaplib/smtplib)
polyrob email

# Discord / Slack / Signal / X — each also runs standalone
polyrob discord
polyrob slack
polyrob signal
polyrob x

# REST API server
polyrob serve

# Web dashboard
polyrob dashboard
```

### Platform Support

| Platform | Hermes | POLYROB |
|----------|--------|---------|
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

**Note:** Discord/Slack/Signal are real thin-client implementations against the real
endpoints (Discord REST v10 + Gateway WS; Slack Web API + Socket Mode; signal-cli
JSON-RPC), unit-tested with mocked transports — "validation pending" means they haven't
been soak-tested against live accounts yet, not that they're stubs. Run them under
`polyrob gateway` (with their flags enabled) or each as its own process
(`polyrob discord` etc.).

---

## Features Unique to Each

### Hermes Has, POLYROB Doesn't

- **Nous Portal** — Single subscription for models/tools
- **Remote execution backends** — SSH, Modal, Daytona (POLYROB's compute-posture
  ladder is Docker-on-the-local-box only)
- **iMessage and IRC surfaces** — POLYROB covers Telegram, WhatsApp, Email, Discord,
  Slack, Signal, and X
- **Companion mobile apps** — iOS/Android nodes
- **Voice modes** — Wake words, continuous voice

### POLYROB Has, Hermes Doesn't

- **Multi-provider automatic failover** — Switch providers on errors
- **Durable goal board** — Goals survive restarts with CAS claims
- **Multi-tenant architecture** — Built for team/business use
- **Three-tier access model** — OWNER/CORRESPONDENT/DENIED
- **Capability gates** — Block high-impact tools for correspondents
- **A2A protocol** — Google's agent interoperability standard
- **REST API** — Built-in HTTP endpoints for programmatic access

---

## Migration Checklist

### Before Migration

- [ ] Identify critical Hermes skills you want to keep
- [ ] Note your current provider/model configuration
- [ ] Document important memory entries
- [ ] Check which messaging platforms you use

### Migration Steps

1. **Install POLYROB:**
   ```bash
   pipx install "polyrob[all]"
   python -m playwright install chromium
   ```

2. **Configure providers:**
   ```bash
   polyrob init
   # Edit ~/.polyrob/.env with your API keys
   ```

3. **Migrate skills:**
   ```bash
   mkdir -p ./.polyrob/skills/user_<your-id>
   cp -r ~/.hermes/skills/user/* ./.polyrob/skills/user_<your-id>/
   ```

4. **Recreate important memories:**
   ```bash
   polyrob chat
   > Tell me the project contexts I need to know
   ```

5. **Set up surfaces:**
   ```bash
   # For Telegram
   polyrob telegram

   # For email
   polyrob email
   ```

6. **Test with familiar tasks:**
   ```bash
   polyrob run "[a task you commonly perform in Hermes]"
   ```

### After Migration

- [ ] Verify all skills load correctly
- [ ] Test provider failover (if using multiple providers)
- [ ] Confirm surfaces work as expected
- [ ] Update any automation/scripts that used Hermes CLI

---

## Example: Full Migration

```bash
# 1. Install POLYROB
pipx install "polyrob[all]"
python -m playwright install chromium

# 2. Copy Hermes config notes
hermes config > ~/hermes-config-notes.txt

# 3. Initialize POLYROB
polyrob init

# 4. Edit ~/.polyrob/.env with your keys
nano ~/.polyrob/.env

# 5. Migrate skills
mkdir -p ./.polyrob/skills/user_<your-id>
cp -r ~/.hermes/skills/user/* ./.polyrob/skills/user_<your-id>/

# 6. Profiles have no 1:1 equivalent — POLYROB isolates by tenant user_id instead

# 7. Test
polyrob doctor
polyrob chat

# 8. Set up surfaces (if used)
polyrob telegram
polyrob email

# 9. Create a test goal (the dispatcher that runs it needs GOALS_ENABLED=true, or POLYROB_LOCAL)
polyrob goals create "Test migration" --body "Summarize my current project context"

# 10. Verify and clean up
# Once satisfied, you can optionally remove Hermes
# hermes gateway stop
```

---

## When to Stay with Hermes

Consider staying with Hermes if:

- **You need Nous Portal** — Single subscription is important to you
- **You need iMessage or IRC** — the two surfaces POLYROB doesn't cover
  (Discord/Slack/Signal/X are covered — see the platform table above)
- **You need remote execution backends** — SSH, Modal, Daytona (POLYROB's compute
  ladder is local-Docker only)
- **You want companion apps** — Mobile/desktop apps are essential

(Agent-created skills are no longer a Hermes exclusive — POLYROB's learning loop ships
behind `SKILLS_WRITABLE`, on by default under `POLYROB_LOCAL`.)

## When to Switch to POLYROB

Consider switching to POLYROB if:

- **You need provider redundancy** — Automatic failover is valuable
- **You run in production** — Multi-tenant architecture and durability matter
- **You value security** — Access control and capability gates are important
- **You want A2A interoperability** — Agent-to-agent communication is needed
- **You need a REST API** — Programmatic access is required

---

## Getting Help

- **Documentation:** [README.md](../../../README.md)
- **Configuration:** [../../CONFIGURATION.md](../../CONFIGURATION.md)
- **Issues:** [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)
- **Comparison:** [../../comparison.md](../../comparison.md)

---

Still deciding? See the [feature comparison](../../comparison.md) for a detailed breakdown of POLYROB vs Hermes and other frameworks.
