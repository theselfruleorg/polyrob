# Skills

A **skill** is a reusable procedure the agent can load on demand — a folder with a
`SKILL.md` file (agentskills.io-compliant Markdown + YAML frontmatter) plus optional
`references/`, `assets/`, and `scripts/` resources. When a task matches a skill's
triggers (or the agent chooses it from the catalog), the agent pulls the skill's full
instructions with the `load_skill` tool and follows them.

POLYROB is an agentskills.io **client**: it discovers and loads skills authored for the
open ecosystem (the same `SKILL.md` format used by Claude Code and other agents), and it
can **install** skills from local folders, GitHub repos, or URLs through a safe,
scanned, quarantined pipeline.

- Skill format / authoring rules: [`docs/SKILL_AUTHORING_STANDARD.md`](../SKILL_AUTHORING_STANDARD.md)
- Config flags and storage details: [`docs/CONFIGURATION.md` → Skills](../CONFIGURATION.md)

---

## Scopes and precedence

Skills come from three kinds of location. When two skills share a name, the
higher-precedence one wins (**project > user > builtin**), and a **builtin is never
shadowed** by an external skill of the same name.

| Scope | Location | Writable? | How it gets there |
|-------|----------|-----------|-------------------|
| **builtin** | the installed package (`data/prompts/skills/`) | read-only, trusted | ships with POLYROB |
| **user** | `<data_home>/skills/user_<uid>/` (local CLI: `./.polyrob/skills/user_<uid>/`, or `$POLYROB_DATA_DIR`) | yes | `polyrob skill install`, or the agent authoring a skill |
| **external (discovered)** | `~/.agents/skills/`, `~/.claude/skills/` (user), and per-repo `./.agents/skills/`, `./.claude/skills/` (project) | no (loaded in place) | drop a skill folder in and it's auto-discovered |

User and installed skills live under your **data home**, not the package tree, so they
**survive `polyrob update`** (the updater snapshots `<data_home>/skills`).

---

## Two ways to add a new skill

There are two distinct paths, and the right one depends on where the skill comes from
and how much vetting you want.

### 1. Discovery — drop-in, zero-install

The fastest way. Put a compliant skill folder in a discovery root and POLYROB picks it
up automatically on the next session — no command, no approval step:

```bash
# any single-skill folder containing a SKILL.md
~/.agents/skills/my-skill/SKILL.md          # available to every local session
~/.claude/skills/my-skill/SKILL.md          # the same folder Claude Code uses — shared
./.agents/skills/my-skill/SKILL.md          # per-repo: only while working in that repo
```

- **Lenient-loaded:** external skills only need a `description`; a non-standard name
  (digit-leading `3d-modeling`, unicode, up to 64 chars) still loads with a warning.
- **Project scope is local-operator only.** `./.agents/skills/` is scanned only on a
  trusted local CLI (`POLYROB_TRUST_PROJECT_SKILLS`, default on locally). On a
  **server it is fail-closed off** — a deployment never scans its working directory for
  skills. See [CONFIGURATION.md](../CONFIGURATION.md).
- Discovered skills are loaded **in place** (never copied); they are **not** threat-scanned
  and do **not** survive being moved — they are the operator's own trusted files.
- **Server note:** the user roots (`~/.agents/skills/`, `~/.claude/skills/`) are the server
  process's home and are **not per-tenant** — anything there is offered to every tenant's
  catalog (read-only, non-executable). Only *project* scope is server-fail-closed. For a
  multi-tenant deployment, prefer per-tenant **install** (below) over dropping files in the
  server home.

Use discovery for skills you author yourself or share with your other agents via
`~/.claude/skills/` / `~/.agents/skills/`.

### 2. Install — managed, scanned, quarantined, audited

Use `polyrob skill install` to bring a skill from a **local folder, a GitHub repo, or a
URL** into your managed user scope. Unlike discovery, install **threat-scans** every
file, **quarantines** the skill for review, records an **audit trail**, and the result
**survives updates**.

```bash
polyrob skill install <spec> [--ref REF] [--trust local|prompt] [--user UID]
```

`<spec>` is auto-detected by shape:

| Spec form | Example | Resolves via |
|-----------|---------|--------------|
| local folder | `polyrob skill install ./my-skill` | filesystem |
| `owner/repo[/subdir]` shorthand | `polyrob skill install acme/skills/pdf` | GitHub clone |
| git URL (`https://`, `git@`, `ssh://`, `file://`) | `polyrob skill install https://github.com/acme/skills.git/pdf` | sandboxed clone |
| direct `SKILL.md` URL | `polyrob skill install https://example.com/pdf/SKILL.md` | HTTP fetch (http/https only) |

Options:
- `--ref REF` — a git branch/tag/commit (git installs only).
- `--trust local` — auto-approve **a local folder you own** (skips quarantine). **Remote
  sources are never auto-approved**, even with `--trust local`.
- `--user UID` — install into a specific tenant (defaults to the local owner).

**The install flow:**

```
polyrob skill install acme/skills/pdf
  → clone (sandboxed) / fetch / read the folder
  → validate the SKILL.md (must have a description; name must be a valid identifier)
  → threat-scan the SKILL.md AND every text resource (fail-closed)
  → copy the whole folder into  <data_home>/skills/user_<uid>/.pending/pdf/
  → prints:  "installed skill 'pdf' to quarantine — run `polyrob skill approve pdf`"

polyrob skill approve pdf
  → promotes the skill to active, ports its resources, records an audit row
  → the skill is now live in your user scope and survives `polyrob update`
```

Review the quarantined skill (it's just files under `.pending/<name>/`) before approving.

---

## Managing skills

```bash
# CATALOG / AUTHORING (the `skills` group)
polyrob skills list                    # list the skill IDs the agent can load
polyrob skills validate [skill-id]     # check authored skills against the SKILL.md standard
polyrob skills export <skill-id> [--to DIR]   # copy a skill out to ~/.agents/skills (portable)

# INSTALL PIPELINE (the `skill` group)
polyrob skill install <spec> [...]     # install (see above)
polyrob skill approve <name>           # activate a quarantined skill
polyrob skill list                     # every scope + status (builtin/user/external × active/pending/archived)
polyrob skill info <skill-id>          # frontmatter + provenance + usage stats
polyrob skill remove <skill-id>        # archive a user skill (recoverable, never hard-deleted)
```

> There are two command groups by design: **`polyrob skills`** (the read/authoring
> surface — list, validate, export) and **`polyrob skill`** (the install pipeline —
> install, approve, list, info, remove). Run either with `--help` for the full surface.

Inside the interactive REPL (`polyrob chat` / `polyrob run`) the same operations are
available as slash commands:

```
/skills                     list every auto-activatable skill
/skills list                full scope/state inventory
/skills info <id>           frontmatter + provenance/usage
/skills install <spec>      install (local operator only)
/skills approve <id>        activate a quarantined skill (local operator only)
/skills remove <id>         archive a user skill
```

---

## Safety model

Installing third-party skills is treated as importing untrusted content. The pipeline
enforces, all fail-closed:

- **Threat scan.** The `SKILL.md` and **every text resource** are scanned for
  prompt-injection before staging; a hit — or a scanner/read error — refuses the install.
- **Quarantine + explicit approve.** Nothing an install brings in goes active until you
  run `skill approve`. Remote sources can never auto-approve.
- **Sandboxed git clone.** Clones run with hooks and system/global config disabled,
  shallow + single-branch, submodules off, with byte/file-count caps and a tree audit
  that **rejects symlinks and path traversal** (checked at the git-object level, not just
  the filesystem). The resolved commit SHA is recorded.
- **URL installs are http/https only** with a size cap and content-type check; the skill
  name from remote frontmatter is sanitized before it touches a path.
- **Scripts are never executed.** `load_skill` and the resource reader **read**
  `scripts/`/`references/`/`assets/` files (realpath-confined to the skill folder, framed
  as untrusted data) — they never run them. Script execution is a separate, gated feature.
- **Server hard-gate.** `skill install` and `skill approve` are **owner/CLI-only**; a
  multi-tenant server refuses them, and there is **no REST install endpoint**. Installed
  skills stay tenant-scoped under `user_<uid>/`.
- **Local-only provenance.** Who authored/installed a skill is recorded in a local
  database, never read from a (forgeable) frontmatter field.
- **Lenient consume, strict author.** Externally-supplied skills are loaded leniently
  (warn, don't reject), but skills POLYROB itself authors must pass strict validation
  (`polyrob skills validate`).

---

## The `SKILL.md` format (quick reference)

```markdown
---
name: pdf-processing                 # required; ≤64 chars; must match the folder name
description: Extract and analyze PDF text. Use when handling PDF files.   # required, ≤1024
license: MIT                         # optional
metadata:                            # optional; string→string map
  polyrob-priority: "3"
  polyrob-auto-activate: "true"
  polyrob-triggers: '{"keywords": ["pdf", "extract"]}'
---

# PDF processing

Step-by-step instructions the agent follows once this skill is loaded…
```

- Only the agentskills.io top-level fields are allowed (`name`, `description`, `license`,
  `compatibility`, `metadata`, `allowed-tools`); POLYROB-specific settings go under
  `metadata` as flat `polyrob-*` string keys, so the file stays portable to other agents.
- Body size: accepted up to 40,000 chars on disk; a warning is logged above ~20,000
  (~5,000 tokens) since that's the injected-body size the standard recommends.
- Full authoring rules and safety conventions: [`docs/SKILL_AUTHORING_STANDARD.md`](../SKILL_AUTHORING_STANDARD.md).

---

## Configuration

The skill-related flags (progressive disclosure, catalog inclusion, writable skills,
project-scope trust, storage location, and size caps) are documented in
[`docs/CONFIGURATION.md` → Skills](../CONFIGURATION.md). The most relevant for adding
skills:

- `POLYROB_TRUST_PROJECT_SKILLS` — trust per-repo `./.agents/skills/` (local default on;
  server forced off).
- `SKILLS_WRITABLE` — let the agent author its own skills (on under `POLYROB_LOCAL`).
- `POLYROB_DATA_DIR` — where user/installed skills are stored.
