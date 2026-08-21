# Profiles

A **profile** is a complete, isolated home for one bot identity: its own `.env`,
characters, skills, identity docs, memory, goals, cron state, and sessions.
Profiles are how you run several bots on one machine, hand an identity to
someone else, or keep your personal bot out of the framework's defaults.

```
~/.polyrob/                        # base home (legacy mode config)
├── active_profile                 # sticky selection (one line)
└── profiles/
    └── rob/                       # a profile == a full POLYROB home
        ├── .env                   # this identity's secrets + flags
        ├── profile.yaml           # name, description, install source
        ├── characters/            # this identity's character files
        ├── skills/
        └── data/                  # identity docs, memory.db, goals.db,
                                   # cron.db, sessions/ — the whole brain
```

## The two modes

**Legacy/project mode** (no profile selected): exactly what POLYROB always did.
Config comes from `~/.polyrob`, data lives in `./.polyrob` under the folder you
launched from, and the persona is the neutral framework agent. Nothing changes
for existing installs.

**Profile mode** (a profile selected): config home = the profile dir, data home
= `<profile>/data`, and the workspace stays in the folder you launched from.

## Selecting a profile

Strongest first:

1. `polyrob -P <name> …` — the CLI flag. Overrides everything, including an
   exported `POLYROB_HOME`.
2. `POLYROB_PROFILE=<name>` env var. Same strength as the flag. Present but
   empty means "no profile" (an escape hatch back to legacy mode).
3. A project pin: a `./.polyrob/profile` file (one line, the profile name),
   found by walking from your current folder up to the git root. A folder can
   *point at* a profile; it never copies one.
4. The sticky file: `polyrob profile use <name>` writes
   `~/.polyrob/active_profile`, and every later run uses it.

The pin and sticky tiers are ambient signals: they never override an explicitly
exported `POLYROB_HOME`/`POLYROB_DATA_DIR` (you get a one-shot warning naming
both instead). That is what keeps a server deployment with an explicit
`POLYROB_DATA_DIR` safe.

## Everyday commands

```bash
polyrob profile create scout --description "research bot"
polyrob -P scout                      # run the REPL as scout
polyrob profile use scout             # make scout sticky for this machine
polyrob profile list                  # * marks the active profile
polyrob profile show [scout]          # homes, instance id, characters, sessions
polyrob doctor                        # prints the active profile + both homes
```

`create` also writes a `~/.local/bin/scout` wrapper by default, so `scout run
"…"` works as a real command (`--no-alias` to skip, `POLYROB_BIN_DIR` to move
it). Each surface is per-profile too: `polyrob -P scout telegram` is scout's
own daemon — one process per profile, no multiplexing.

## Moving an existing folder-bot into a profile

Your existing `./.polyrob` folder data is never moved. To formalize it:

```bash
cd ~/my-bot-folder
polyrob profile adopt mybot           # copies identity out, pins the folder
polyrob profile adopt mybot --include-data   # also copy memory/goals/cron DBs
```

`adopt` copies identity docs, characters, and the identity-shaped env keys
(`POLYROB_INSTANCE_ID`, `POLYROB_PERSONA`, …) into the new profile and writes
the `./.polyrob/profile` pin. The legacy folder keeps working as the backup.

## Sharing an identity

Two distinct mechanisms:

**Export/import** — backup, or moving between your own machines:

```bash
polyrob profile export rob -o rob.tar.gz
polyrob profile import rob.tar.gz --name rob2
```

Credentials never enter an export: `.env`, `auth.json`, and wallet material are
excluded, and every text file in the archive is scrubbed for secret-shaped
strings. Re-add keys on the importing machine (`polyrob init --profile rob2`).

**Install/update** — the shareable distribution format. A distribution is a git
repo (or local dir) with a `polyrob.profile.yaml` manifest:

```bash
polyrob profile install https://github.com/you/scout-profile#v1.2
polyrob profile update scout          # re-fetch, replace owned files
polyrob profile info scout            # manifest, source, required env keys
```

The ownership contract: the distribution owns `characters/`, `skills/`,
`cron/`, `mcp.json`, and a shipped `soul.md` — those are replaced on update.
`config.yaml` is preserved unless you pass `--force-config`. Your `.env`,
`auth.json`, wallet, and the whole `data/` tree (memory, sessions, the agent's
own `self.md`) are **never touched**: a distribution ships a soul, never
someone else's memories.

## Daemons

One systemd unit per profile. Let the CLI emit one matched to your layout:

```bash
polyrob profile create scout --service      # writes polyrob-scout.service
sudo cp <emitted unit> /etc/systemd/system/ && sudo systemctl enable --now polyrob-scout
```

The unit sets `POLYROB_PROFILE` and `POLYROB_PROFILES_ROOT` explicitly — the
strong env tier that the CLI resolves at process start; without them a spawned
daemon runs in legacy mode and writes into the default home. Two rules:

- The unit's `POLYROB_PROFILES_ROOT` must name the registry the profile
  actually lives in (the CLI default is `~/.polyrob/profiles`). Create server
  profiles under the server root:
  `POLYROB_PROFILES_ROOT=/var/lib/polyrob/profiles polyrob profile create <name>`.
- Give each profile its own surface credentials in its `.env` (e.g. its own
  Telegram bot token) — two daemons long-polling one token fight each other.

(The source repo also carries a generic template, `deployment/polyrob@.service`,
for `systemctl enable polyrob@<name>`-style instancing.)

## Guard rails

- Profile names are `[A-Za-z0-9_-]`, max 64 chars — rejected, never rewritten.
- A session running as one profile cannot read or write another profile's home
  through the file tools (defense-in-depth, not a security boundary; bypass
  deliberately with `POLYROB_ALLOW_CROSS_PROFILE=1`).
- A process that reaches the runtime without profile resolution while the
  sticky file names a profile prints a loud one-shot warning: anything it
  writes would land in the DEFAULT home, not the profile.
