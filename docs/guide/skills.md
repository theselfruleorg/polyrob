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

Skills come from the scopes below. Resolution order is **project > user > builtin
> pack**, with protected builtin names: a **builtin is never shadowed** by an
external skill of the same name. A colliding pack skill uses `<pack-id>:<skill-id>`.

| Scope | Location | Writable? | How it gets there |
|-------|----------|-----------|-------------------|
| **builtin** | the installed package (`data/prompts/skills/`) | read-only, trusted | ships with POLYROB |
| **pack:<id>** | a loaded pack's contributed skills directory | read-only, trusted with the loaded pack | installed with the pack; absent when the pack is disabled/refused |
| **user** | `<data_home>/skills/user_<uid>/` (local CLI: `./.polyrob/skills/user_<uid>/`, or `$POLYROB_DATA_DIR`) | yes | `polyrob skill install`, or the agent authoring a skill |
| **external (discovered)** | `~/.agents/skills/`, `~/.claude/skills/` (user), and per-repo `./.agents/skills/`, `./.claude/skills/` (project) | no (loaded in place) | drop a skill folder in and it's auto-discovered |

Builtin skills ship inside the installed package (`data/prompts/skills/`) and are
replaced wholesale by an update. User and installed skills live under your **data
home**, not the package tree, so they **survive `polyrob update`** — the updater
snapshots `<data_home>/skills` first, and `polyrob update --rollback` restores it
(data only; the code is not reverted — see
[upgrading.md](upgrading.md#the-safety-net)).

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

Use `polyrob skill install` to bring a skill from a **tap, a local folder, a GitHub repo,
or a URL** into your managed user scope. Unlike discovery, install **threat-scans** every
file, **quarantines** the skill for review, records an **audit trail**, and the result
**survives updates**.

```bash
polyrob skill install <spec> [--ref REF] [--trust local|prompt] [--user UID]
```

`<spec>` is auto-detected by shape:

| Spec form | Example | Resolves via |
|-----------|---------|--------------|
| tap name `<tap>/<skill>` | `polyrob skill install anthropics/skills/pdf` | the tap's index, then a GitHub clone |
| bare skill name | `polyrob skill install pdf` | every tap; refused if more than one tap has it |
| local folder | `polyrob skill install ./my-skill` | filesystem |
| `owner/repo[/subdir]` shorthand | `polyrob skill install acme/skills/pdf` | GitHub clone |
| git URL (`https://`, `git@`, `ssh://`, `file://`) | `polyrob skill install https://github.com/acme/skills.git/pdf` | sandboxed clone |
| direct `SKILL.md` URL | `polyrob skill install https://example.com/pdf/SKILL.md` | HTTPS fetch (https only) |

A tap name is tried first. `owner/repo/<path>` for a repo that is not a tap is a plain
GitHub clone, as before.

Options:
- `--ref REF` — a git branch/tag/commit (git installs only).
- `--trust local` — auto-approve **a local folder you own** (skips quarantine). **A git
  or URL spec you type is never auto-approved**, even with `--trust local`. A name from an
  official or trusted tap auto-approves on a clean scan (see the trust table below).
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

### Taps and search

A **tap** is a GitHub repo (or a folder in one) that publishes skills. Two taps are
built in and cannot be removed:

| Tap | Trust |
|-----|-------|
| `theselfruleorg/polyrob-skills` | official |
| `anthropics/skills` | trusted |

```bash
polyrob skill tap list
polyrob skill tap add acme/skills[/subdir]     # your taps are "community"
polyrob skill tap remove acme/skills
polyrob skill search pdf [--refresh]
```

Search reads the tap's `skills/index.json` (a list of `{name, description, path}`, where
`path` is relative to the tap). A tap without an index is listed from its `SKILL.md`
folders through the GitHub API. Results are cached under `<data_home>/skills/.hub/cache/`
for an hour; `--refresh` skips the cache. A tap that cannot be reached prints one line
that names it, and search continues with the other taps. There are no aggregator
sources (no ClawHub, no skills.sh).

### Trust

| Where the skill comes from | clean scan | caution | scan hit |
|---|---|---|---|
| a local folder, `--trust local` | active | quarantine | refused |
| a local folder, default | quarantine | quarantine | refused |
| an official or trusted tap (by name) | active | quarantine | refused |
| a community tap, a git spec, a URL | quarantine | quarantine | refused |

A scan hit is always refused; there is no `--force`. The scanner gives only "clean" or
"hit" today, so the caution column is not used yet. Every approval, automatic or not, is
recorded in the install audit.

### The skill lock

Every approval writes a row to `<data_home>/skills/.hub/lock.json`:

```json
{"version": 2, "installed": {"user_local/pdf": {
  "source": "git:anthropics/skills/skills/pdf", "tap": "anthropics/skills",
  "ref_sha": "<commit>", "trust": "trusted", "scan_verdict": "safe",
  "content_sha256": "<hash of the fetched files>", "files": {"SKILL.md": "<sha256>"},
  "installed_at": "2026-09-24T10:40:50Z",
  "install_path": "<data_home>/skills/user_local/pdf", "user_id": "local"}}}
```

Keys include the tenant and skill name, so installing or removing a same-named
skill for one user preserves every other user's lock. Version 1 rows are read
under the tenant in their validated install path and migrate on the next write.

`skill remove` deletes that user's row. POLYROB checks every row when it writes and when it
reads: the name must be a valid skill id, and `install_path` must be
`<data_home>/skills/user_<uid>/<name>`. A row that fails is ignored and logged, never
used.

### Update

```bash
polyrob skill update [name]
```

`update` fetches each locked skill again from its source at the latest commit (a tap
skill from the tap, a git or URL install from the same spec). The same scan and trust
rules apply. When the files are the same, it prints `up-to-date`. When they changed, it
prints the old and new content hash and each file changed, added or removed. Then the
new version goes active (official or trusted tap) or waits in quarantine for
`skill approve`. A local-folder install is skipped: install the folder again.

---

## Managing skills

There are **two command groups**, and `polyrob --help` prints them as one row,
`skills (alias: skill)`:

- **`polyrob skills`** — the read/authoring surface: `list`, `validate`, `export`.
- **`polyrob skill`** — the install pipeline: `install`, `approve`, `list`, `info`,
  `remove`, `tap`, `search`, `update`.

Both names always work. Run either with `--help`, or see [cli.md](cli.md) for the
full command reference.

Inside the interactive REPL (`polyrob chat` / `polyrob run`) the same operations are
available as slash commands:

```
/skills                     list every auto-activatable skill
/skills list                full scope/state inventory
/skills info <id>           frontmatter + provenance/usage
/skills install <spec>      install (local operator only)
/skills approve <id>        activate a quarantined skill (local operator only)
/skills remove <id>         archive a user skill
/skills tap [list|add|remove] [owner/repo]   manage taps (add/remove: local operator only)
/skills search <query>      search the taps
/skills update [id]         update installed skills (local operator only)
```

---

## Safety model

Installing third-party skills is treated as importing untrusted content. The pipeline
enforces, all fail-closed:

- **Threat scan.** The `SKILL.md` and **every text resource** are scanned for
  prompt-injection before staging; a hit — or a scanner/read error — refuses the install.
- **Quarantine + explicit approve.** Nothing an install brings in goes active until you
  run `skill approve`, except a local folder with `--trust local` and a clean skill
  from an official or trusted tap (see Trust above).
- **Sandboxed git clone.** Clones run with hooks and system/global config disabled,
  shallow + single-branch, submodules off, with byte/file-count caps and a tree audit
  that **rejects symlinks and path traversal** (checked at the git-object level, not just
  the filesystem). The resolved commit SHA is recorded.
- **URL installs are https only** with a size cap and content-type check; the skill
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
name: pdf-processing                 # required; must match the folder name
description: Extract and analyze PDF text. Use when handling PDF files.   # required
metadata:                            # optional; POLYROB settings live here
  polyrob-auto-activate: "true"
  polyrob-triggers: '{"keywords": ["pdf", "extract"]}'
---

# PDF processing

Step-by-step instructions the agent follows once this skill is loaded…
```

Only the agentskills.io top-level fields are allowed (`name`, `description`,
`license`, `compatibility`, `metadata`, `allowed-tools`), so the file stays
portable to other agents; POLYROB settings go under `metadata` as flat
`polyrob-*` string keys. `allowed-tools` is advisory in POLYROB: when the skill loads,
the agent sees one line that names the declared tools, but POLYROB does not remove any
other tool from the session. Field limits, trigger syntax and the safety
conventions are in
[`docs/SKILL_AUTHORING_STANDARD.md`](../SKILL_AUTHORING_STANDARD.md) — write to
that, and check your work with `polyrob skills validate`.

---

## Configuration

The skill-related flags (progressive disclosure, catalog inclusion, writable skills,
project-scope trust, storage location, and size caps) are documented in
[`docs/CONFIGURATION.md` → Skills](../CONFIGURATION.md). The most relevant for adding
skills:

- `POLYROB_TRUST_PROJECT_SKILLS` — trust per-repo `./.agents/skills/` (local default on;
  server forced off).
- `SKILLS_WRITABLE` — let the agent author its own skills. It is on by default only
  when `POLYROB_LOCAL` **and** `AUTONOMY_ENABLED` are both on (it belongs to the
  autonomy group, not the interactive one — see
  [configuration.md](configuration.md#6-the-autonomy-dial)). What the agent writes
  is quarantined for your review; `polyrob owner pending` lists it.
- `POLYROB_DATA_DIR` — where user/installed skills are stored.

Run `polyrob doctor --flags --group skills` for the live values on your box.
