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
polyrob identity avatar    # the avatar image (set from a file, a URL or an NFT)
```

`polyrob soul`, `polyrob persona` and `polyrob avatar` remain invocable at those
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
axis: `POLYROB_OWNER_USER_ID` (the retired `BOT_OWNER_USER_ID` refuses to start), else `POLYROB_LOCAL_OWNER`,
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

That writes two files under your data home (local CLI default: `~/.polyrob/data`;
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

By default an agent's change to its `self.md` in a scheduled run is a draft you approve
(`/pending`). Set the preference `self.autonomous_writes` to `true` to let the agent's own scheduled jobs
and goals write it at once: each such write must quote the line of the SOUL it serves, the
old text is archived, and you get one message per change. A group chat, a helper agent or a
self-wake still writes only drafts.

### Public knowledge

The SOUL never reaches a group or a stranger's chat. What any stranger may know goes
in a third operator file, `<data_home>/identity/public.md` (at most 6000 chars): what
the agent and its organization are, the public links, where questions go, and what it
never shares. Every chat gets it, a group and a person who is not the owner included.
Put product facts here, never in the character (the character is the voice). Name the
official page for each fact, and list the plain-text pages the agent may fetch for a
detail the file does not hold.

The agent cannot write `identity.md`, `operating.md` or `public.md` in
`<data_home>/identity/` with its file abilities: only you write them.

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

The layers, highest authority first:

| Layer | Where | Selected by | Who writes it |
|---|---|---|---|
| SOUL | `<data_home>/identity/identity.md` + `operating.md` | the files existing | operator only |
| Public knowledge | `<data_home>/identity/public.md` (every chat, groups too) | the file existing | operator only |
| Character | `<characters_dir>/<slug>.character.json` | `PERSONALITY_DEFAULT_CHARACTER` | operator |
| Persona override | — | the owner's `/persona <name>` (the `session.persona` pref), else `POLYROB_PERSONA` | owner, else operator |

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
assembled once, at agent creation. `/persona <name>` works in each chat app and the
terminal, applies to every new chat, and wins over `POLYROB_PERSONA`;
`/persona reset` removes it.

`polyrob doctor` prints which character is live and the file it resolved to;
`/persona` shows the active persona in each chat app, and `/persona list` in the terminal
lists both namespaces and marks the active row.

#### Which character fields reach the model

The persona block reaches every place where the agent speaks: each chat app,
the REPL and `polyrob run`, a scheduled job, a goal, a helper the agent starts,
an agent-to-agent request and the `/v1` API. It renders a deliberate subset of
the character:

| Field | Rendered into the persona block? |
|---|---|
| `name` | yes — `You are <name> — <adjectives>.` |
| `adjectives` | yes — folded into the name line |
| `bio` | yes |
| `lore` | yes — `Background: …` |
| `topics` | yes — `You focus on: …` |
| `style.all`, `style.chat`, `style.speaking`, `style.writing` | yes — `Style: …` |
| `postExamples`, `messageExamples` | yes, a small sample — the first 3 of each, each cut at 240 characters, as `Voice samples` (tone only). In the dialogue shape only the character's own lines count |
| `knowledge` | **no** — stored only (facts belong to the identity docs) |
| `modelProvider`, `clients`, `settings` | no — not persona text |

The SSOT for this split is `RENDERED_FIELDS` / `STORED_ONLY_FIELDS` in
`agents/personality/persona_render.py`. A character that fills a stored-only
field logs a one-time warning, and `polyrob doctor --full` and `polyrob persona
show` name the field. Write examples that show the voice, not facts: the model
reads them as tone.

---

## The default instance: `polyrob`

| Property | Value |
|----------|-------|
| Instance ID | `polyrob` (an unset/blank `POLYROB_INSTANCE_ID` — with no active profile — always degrades to this) |
| CLI config home | `~/.polyrob/` — `.env`, `cli.json`, `mcp.json` (fixed; not instance-scoped) |
| Data home (local/CLI, default) | `~/.polyrob/data/` (`<POLYROB_HOME>/data`). A `./.polyrob/` data folder from a release before 1.3 is not read; the CLI names it in a warning |
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
see [profiles.md](profiles.md). The manual recipe below shows the underlying
mechanics. `POLYROB_INSTANCE_ID` alone doesn't separate data, and neither does the
directory you launch from (the local data home is always `<POLYROB_HOME>/data`), so
give `aria` its own home with `POLYROB_HOME`:

### Example: a second instance named `aria`

1. Create a home for `aria`:

   ```bash
   export POLYROB_HOME=~/.polyrob-aria
   POLYROB_INSTANCE_ID=aria polyrob init
   ```

2. Start the REPL as `aria` (with the same `POLYROB_HOME`):

   ```bash
   POLYROB_INSTANCE_ID=aria polyrob
   ```

3. Run a task as `aria`:

   ```bash
   POLYROB_INSTANCE_ID=aria polyrob run "compile a market summary"
   ```

`POLYROB_DATA_DIR` moves only the data home; `POLYROB_HOME` moves the `.env` and
the data home together.

### Running two instances simultaneously on a server

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
partitioned by `POLYROB_INSTANCE_ID`. Two instances that share a data home (the
same `POLYROB_HOME` locally, or the same `POLYROB_DATA_DIR` on a server) read and
write the *same* memory, skills, cron jobs, and goal board — only their
self-identity is separate. The local CLI uses one data home whatever directory you
launch it from. Give each instance its own data home (as shown above)
for full separation.

- **Separate memory, skills, cron/goal state, and auth DB** — requires a distinct
  `POLYROB_HOME` (or `-P <name>`) on the CLI, or a distinct `POLYROB_DATA_DIR` on a
  server. A different working directory separates nothing.
- **Separate SELF identity** — the agent's own evolving SELF docs *are* already
  instance-scoped within a shared data home
  (`<data_home>/identity/{instance_id}/user_{user_id}/`), so even instances that
  share a data home never blend their evolving self-context. The operator-authored
  SOUL docs (`<data_home>/identity/identity.md` + `operating.md`) are flat — two
  instances sharing one data home share one SOUL; give each its own data home if
  their SOULs must differ.
- **LLM provider keys** (`ANTHROPIC_API_KEY` etc.) are process-wide env vars —
  shared across instances unless you launch each from a separate environment file.
