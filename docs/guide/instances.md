# Instances

## Framework vs instance

**polyrob** is the framework — the Python package, the CLI, and the agent runtime.

An **instance** is a named deployment of the framework with its own self-identity.
A fresh install runs the neutral default instance, `polyrob` — no person's bot
ships in the package. A specific bot (its character, identity docs, memory) is
*data* that lives in a data home or a named profile.

The recommended way to run multiple named instances on one machine is
**[profiles](profiles.md)** — `polyrob profile create <name>` gives each bot a
fully isolated home (config + identity + memory + scheduled work), selected with
`polyrob -P <name>`. The rest of this page describes the lower-level instance-id
mechanics that profiles build on.

---

## Instance identity

Each instance is identified by its **instance ID** (`POLYROB_INSTANCE_ID`, default
`polyrob`; in profile mode it defaults to the profile's name). Today, the
instance ID determines:

- The name the agent uses for itself (CLI banners, `/session`, `/self`)
- The path for the agent's own evolving SELF docs (`self.md`, `owner.md`,
  `contract.md`), nested under the shared data home:
  `<data_home>/identity/{instance_id}/user_{user_id}/`.
  **The operator-authored SOUL docs are NOT nested** — they live flat at
  `<data_home>/identity/identity.md` and `<data_home>/identity/operating.md`
  (a SOUL file placed in the nested per-user directory never loads)
- The instance's owner principal, when `POLYROB_OWNER_USER_ID` isn't set explicitly

It does **not** currently partition memory, skills, cron/goal state, or the auth
database — see [Instance isolation](#instance-isolation) below for what actually
separates two instances.

### Authoring your instance's SOUL

The SOUL is the operator-authored, frozen identity layer — who this instance *is*
(mission, values, boundaries). The agent can never edit it; it's pinned into every
session as a foundation message. To author it:

```bash
# 1. Find your data home (local CLI default: ./.polyrob under your working dir;
#    server: $POLYROB_DATA_DIR)
mkdir -p <data_home>/identity

# 2. Who the instance is — mission, personality, values
$EDITOR <data_home>/identity/identity.md

# 3. (Optional) How it operates — standing constraints, escalation rules, tone
$EDITOR <data_home>/identity/operating.md
```

Both files are plain Markdown, loaded in that order (identity first) and capped at
~60k chars combined. They take effect at the next session start — no restart of
anything else needed. Blank or missing files are simply skipped (a fresh install has
no SOUL and behaves identically).

The agent's own evolving **SELF** docs (`self.md`, plus `owner.md` owner-facts and
`contract.md`) are separate: agent-written through a quarantine-and-scan pipeline,
stored per-user under `identity/{instance_id}/user_{uid}/`. Author the SOUL; let the
agent earn the SELF.

---

## The default instance: `polyrob`

| Property | Value |
|----------|-------|
| Instance ID | `polyrob` (an unset/blank `POLYROB_INSTANCE_ID` — with no active profile — always degrades to this) |
| CLI config home | `~/.polyrob/` — `.env`, `cli.json`, `mcp.json` (fixed; not instance-scoped) |
| Data home (local/CLI, default) | `./.polyrob/` under the current working directory |
| Data home (explicit, any mode) | `$POLYROB_DATA_DIR`, when set — the recommended way to pin a server deployment's data home |
| Memory DB | `<data_home>/memory.db` |

See [self-hosting.md](self-hosting.md) and [configuration.md](configuration.md) for
the full data-home story.

---

## Selecting an instance

Set the `POLYROB_INSTANCE_ID` environment variable before running any polyrob command or starting the server:

```bash
# Run as the default 'polyrob' instance (no env var needed)
polyrob run "summarize this week's news"

# Run as a different named instance
POLYROB_INSTANCE_ID=aria polyrob run "write a daily briefing"

# Or export for the current shell session
export POLYROB_INSTANCE_ID=aria
polyrob
```

---

## Running a second named instance

**Prefer a profile:** `polyrob profile create aria && polyrob -P aria` gives
`aria` an isolated home (config, identity, memory, cron/goals) in one step —
see [profiles.md](profiles.md). The manual recipe below still works and shows
the underlying mechanics: the instance ID alone doesn't separate data, so give
`aria` its own data home by running it from its own working directory:

### Example: a second instance named `aria`

1. Create a config for the new instance, from a dedicated directory:

   ```bash
   mkdir aria-instance && cd aria-instance
   POLYROB_INSTANCE_ID=aria polyrob init
   ```

2. Start the REPL as `aria` (from that same directory):

   ```bash
   POLYROB_INSTANCE_ID=aria polyrob
   ```

3. Run a task as `aria`:

   ```bash
   POLYROB_INSTANCE_ID=aria polyrob run "compile a market summary"
   ```

### Running two instances simultaneously (server mode)

On the server, a working directory isn't the natural unit of isolation — use a
distinct `POLYROB_DATA_DIR` per instance instead, plus a distinct port and env file:

```bash
# Terminal 1 — default instance on port 9000 (default)
POLYROB_INSTANCE_ID=rob python main.py

# Terminal 2 — second instance on port 9001, its OWN data home
POLYROB_INSTANCE_ID=aria UVICORN_PORT=9001 POLYROB_DATA_DIR=/var/lib/polyrob-aria python main.py
```

Or use separate Docker Compose services, one per instance, each with its own
`env_file` (setting a distinct `POLYROB_INSTANCE_ID` and `POLYROB_DATA_DIR`) and
volume mount pointing to a different data directory.

---

## Instance isolation

**Isolation today rides the data home, not the instance ID.** Memory, skills,
cron jobs, the goal board, and the auth/API-key database are keyed by **user ID**
and live in one shared `<data_home>` per process — none of them are currently
partitioned by `POLYROB_INSTANCE_ID`. Two instances that share a data home (same
working directory locally, or the same `POLYROB_DATA_DIR` on a server) read and
write the *same* memory, skills, cron jobs, and goal board — only their
self-identity is separate. Give each instance its own data home (as shown above)
for full separation.

- **Separate memory, skills, cron/goal state, and auth DB** — requires a distinct
  working directory (local/CLI) or a distinct `POLYROB_DATA_DIR` (server) per
  instance.
- **Separate SELF identity** — the agent's own evolving SELF docs *are* already
  instance-scoped within a shared data home
  (`<data_home>/identity/{instance_id}/user_{user_id}/`), so even instances that
  share a data home never blend their evolving self-context. The operator-authored
  SOUL docs (`<data_home>/identity/identity.md` + `operating.md`) are flat — two
  instances sharing one data home share one SOUL; give each its own data home if
  their SOULs must differ.
- **LLM provider keys** (`ANTHROPIC_API_KEY` etc.) are process-wide env vars —
  shared across instances unless you launch each from a separate environment file.
