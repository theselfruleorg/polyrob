# Instances

An **instance** is a named deployment of the framework with its own self-identity.
A fresh install runs the neutral default instance, `polyrob` — no person's bot
ships in the package.

To run several bots on one machine, use **[profiles](profiles.md)**: one command
gives each bot a fully isolated home. This page covers the layer underneath —
the instance id, and the identity documents that make an instance a particular
agent rather than the framework default.

Three commands sit under one umbrella, `polyrob identity`:

```bash
polyrob identity soul      # the operator-authored, frozen identity documents
polyrob identity persona   # the character (voice)
polyrob identity avatar    # the generated face
```

`polyrob soul`, `polyrob persona` and `polyrob pfp` remain invocable at those
names; they are folded onto one row in `polyrob --help`.

---

## Instance identity

Each instance is identified by its **instance ID** (`POLYROB_INSTANCE_ID`, default
`polyrob`; with an active profile it defaults to the profile's name). The
instance ID determines:

- The name the agent uses for itself (CLI banners, `/session`, `/self`)
- The path for the agent's own evolving SELF docs (`self.md`, `owner.md`,
  `contract.md`), nested under the shared data home:
  `<data_home>/identity/{instance_id}/user_{user_id}/`.
  **The operator-authored SOUL docs are NOT nested** — they live flat at
  `<data_home>/identity/identity.md` and `<data_home>/identity/operating.md`
  (a SOUL file placed in the nested per-user directory never loads)

**The instance ID is not the owner tenant.** Memory rows, goals, invoices and
preferences are written under an owner *tenant*, and that is resolved on its own
axis: `POLYROB_OWNER_USER_ID` (or `BOT_OWNER_USER_ID`), else `POLYROB_LOCAL_OWNER`,
else the single-user tenant `local` — never the instance id. Naming an instance
therefore moves nothing a session wrote. If your install predates this and stored
rows under the instance id as a tenant, bind that value back with
`POLYROB_OWNER_USER_ID=<instance id>`; see [upgrading.md](upgrading.md).

The instance ID does **not** partition memory, skills, or cron/goal state either
— see [Instance isolation](#instance-isolation) below for what actually separates
two instances.

### Authoring your instance's SOUL

The SOUL is the operator-authored, frozen identity layer — who this instance *is*
(mission, values, boundaries). The agent can never edit it; it's pinned into every
session as a foundation message. Scaffold it:

```bash
polyrob identity soul init          # asks for a name and a one-line mission, then opens $EDITOR
```

That writes two files under your data home (local CLI default: `./.polyrob`;
server: `$POLYROB_DATA_DIR`), which you can also edit by hand:

```bash
$EDITOR <data_home>/identity/identity.md    # who the instance is — mission, personality, values
$EDITOR <data_home>/identity/operating.md   # optional: standing constraints, escalation rules, tone
```

Both files are plain Markdown, loaded in that order (identity first) and capped at
~60k chars combined. They take effect at the next session start — no restart of
anything else needed. Blank or missing files are simply skipped (a fresh install has
no SOUL and behaves identically).

The agent's own evolving **SELF** docs (`self.md`, plus `owner.md` owner-facts and
`contract.md`) are separate: agent-written through a quarantine-and-scan pipeline,
stored per-user under `identity/{instance_id}/user_{uid}/`. Author the SOUL; let the
agent earn the SELF.

### Giving your instance a character

Below the SOUL sits the **character** — the voice. It is a JSON file named
`<slug>.character.json`, and it is selected by `PERSONALITY_DEFAULT_CHARACTER`.

```bash
polyrob identity persona list                     # templates + characters, active marked
polyrob identity persona init mybot --from writer # scaffold, select it, open $EDITOR
polyrob identity persona show                     # exactly what the model receives
polyrob doctor                                    # which character is live, and its file
```

`polyrob init` offers this as a step (`--character <slug>` non-interactively),
so a fresh install never has to assemble it by hand.

The three layers, highest authority first:

| Layer | Where | Selected by | Who writes it |
|---|---|---|---|
| SOUL | `<data_home>/identity/identity.md` + `operating.md` | the files existing | operator only |
| Character | `<characters_dir>/<slug>.character.json` | `PERSONALITY_DEFAULT_CHARACTER` | operator |
| Persona override | — | `POLYROB_PERSONA`, or the `session.persona` pref (`/persona`) | operator |

Character directories are searched highest-first:

1. `<data_home>/characters/`
2. `<config_home>/characters/` — a profile ships its characters beside its `.env`
3. `<install root>/data/characters/` — the curated presets
   (`analyst`, `coder`, `default`, `ops`, `researcher`, `writer`)
4. the packaged neutral `polyrob` character

`POLYROB_PERSONA` and `/persona <value>` share ONE resolution order:
**template key → character slug → literal free-form text.** So `/persona
researcher` selects the `researcher` character, `/persona coding` selects the
built-in `coding` template, and anything else is used verbatim as persona text.
Every one of them applies to the **next** session — the `<identity>` block is
assembled once, at agent creation.

`polyrob doctor` prints which character is live and the file it resolved to;
`/persona` lists both namespaces and marks the active row.

#### Which character fields reach the model

The persona block renders a deliberate subset. The other fields parse and store
fine but are consumed by nothing on this path — an authored value there is a
silent no-op:

| Field | Rendered into the persona block? |
|---|---|
| `name` | yes — `You are <name> — <adjectives>.` |
| `adjectives` | yes — folded into the name line |
| `bio` | yes |
| `lore` | yes — `Background: …` |
| `topics` | yes — `You focus on: …` |
| `style.all`, `style.chat`, `style.speaking` | yes — `Style: …` |
| `knowledge` | **no** — stored only |
| `messageExamples` | **no** — stored only |
| `postExamples` | **no** — stored only |
| `style.writing` | **no** — stored only |
| `modelProvider`, `clients`, `settings` | no — not persona text |

The SSOT for this split is `RENDERED_FIELDS` / `STORED_ONLY_FIELDS` in
`agents/personality/persona_render.py`, and a character that populates a
stored-only field logs a one-time warning naming it. Put the voice you want the
model to have in `bio`, `lore` and `style`.

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
POLYROB_DATA_DIR=/var/lib/polyrob python main.py

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
