# Instances

## Framework vs instance

**polyrob** is the framework — the Python package, the CLI, and the agent runtime.

**rob** is the default instance — a named deployment of the framework with its own self-identity. When you install polyrob and run `polyrob`, you are running the `rob` instance.

This model lets you run multiple named instances of polyrob on the same machine or
server, each with its own name and self-context. Give each instance its own **data
home** too (see [Instance isolation](#instance-isolation) below) if you want their
memory, skills, and scheduled work fully separated.

---

## Instance identity

Each instance is identified by its **instance ID** (`POLYROB_INSTANCE_ID`, default
`rob`). Today, the instance ID determines:

- The name the agent uses for itself (CLI banners, `/session`, `/self`)
- The path for the operator-authored SOUL docs and the agent's own evolving SELF
  doc, nested under the shared data home: `<data_home>/identity/{instance_id}/user_{user_id}/`
- The instance's owner principal, when `POLYROB_OWNER_USER_ID` isn't set explicitly

It does **not** currently partition memory, skills, cron/goal state, or the auth
database — see [Instance isolation](#instance-isolation) below for what actually
separates two instances.

---

## The default instance: `rob`

| Property | Value |
|----------|-------|
| Instance ID | `rob` (an unset/blank `POLYROB_INSTANCE_ID` always degrades to this) |
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
# Run as the default 'rob' instance (no env var needed)
polyrob run "summarize this week's news"

# Run as a different named instance
POLYROB_INSTANCE_ID=aria polyrob run "write a daily briefing"

# Or export for the current shell session
export POLYROB_INSTANCE_ID=aria
polyrob
```

---

## Running a second named instance

`polyrob init` has no instance-aware flag, and (per [Instance isolation](#instance-isolation)
below) the instance ID alone doesn't separate data — so give `aria` its own data
home by running it from its own working directory:

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
- **Separate self-identity** — the operator-authored SOUL docs and the agent's own
  evolving SELF doc *are* already instance-scoped within a shared data home
  (`<data_home>/identity/{instance_id}/user_{user_id}/`), so even instances that
  share a data home never blend their self-context.
- **LLM provider keys** (`ANTHROPIC_API_KEY` etc.) are process-wide env vars —
  shared across instances unless you launch each from a separate environment file.
