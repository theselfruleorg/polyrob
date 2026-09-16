# Self-Hosting

Run polyrob on your own server, either in Docker or as systemd services. This page
covers the persistent, always-on shape; for installing it on your own machine with
pip or pipx, start at [getting-started.md](getting-started.md).

> For the **web console** specifically — which posture to run it in, owner login,
> and the multi-tenant ceiling — see
> [deployment-postures.md](deployment-postures.md).

---

## Quick start (Docker)

You need Docker ≥ 24 with Compose V2, and at least one LLM provider API key
(see [configuration.md](configuration.md)).

### 1. Clone the repository

```bash
git clone https://github.com/theselfruleorg/polyrob
cd polyrob
```

### 2. Configure your environment

```bash
cp config/.env.example config/.env.development
$EDITOR config/.env.development
```

Set at least one provider key (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, etc.). See [configuration.md](configuration.md) for all options.

`docker-compose.yml` ships pointing `env_file` at the tracked `config/.env.example` so a fresh clone's `docker compose up` resolves without error. Before running for real, edit the `env_file:` line in `docker-compose.yml` to point at `config/.env.development` instead — that keeps your real key out of a git-tracked file.

### 3. Start with Docker Compose

```bash
docker compose up
```

This builds the image (if not cached), installs the `server`, `browser`, and `memory-vector` extras, runs `python -m playwright install --with-deps chromium`, and starts the FastAPI server. Docker sets `UVICORN_PORT=8000` and maps 8000:8000; `curl http://localhost:8000/docs` works.

For detached (background) mode:

```bash
docker compose up -d
```

### 4. Verify it's running

```bash
curl http://localhost:8000/docs
# Should return the interactive Swagger UI HTML
```

---

## What the Compose setup includes

The `Dockerfile` builds a production image with:

- `polyrob[server,browser,memory-vector]` extras installed
- Playwright Chromium binary pre-installed
- `python main.py` as the entrypoint (`UVICORN_PORT=8000` set by the image)

The `docker-compose.yml` file:

- Maps port `8000:8000`
- Loads env vars via `env_file`, pointed at `config/.env.example` out of the box (repoint it at `config/.env.development` once you've added real secrets — see step 2)
- Mounts `./.polyrob` into the container at `/app/.polyrob` for persistent memory and session data

Instance data (memory, sessions, skills, cron jobs) survives container restarts because it is stored in the `./.polyrob` volume mount.

---

## Persistent data

| Host path | Container path | Contents |
|-----------|---------------|---------|
| `./.polyrob/` | `/app/.polyrob/` | Memory DB, sessions, skills, cron jobs (the server-side data home; set via `POLYROB_DATA_DIR` in `config/.env.example`) |

To back up your instance data, copy the `.polyrob/` directory.

---

## Durability & session resume

Session state is stored on disk, so a session survives a process restart. When
the API restarts and a new message arrives for an existing session:

- **Session metadata is reloaded from disk** at startup (`SessionManager`
  rebuilds its index from each session's `metadata.json` under the data home).
- **The orchestrator is recreated on demand.** The live in-memory orchestrator
  does not survive a restart, so the first message to an old session recreates it
  from that session's persisted `request`/`config` and **restores its message
  history from disk** (`message_history.json`) plus any queued HITL messages
  (`hitl_state.json`).
- **A crash-interrupted session resumes.** A session left `status="running"` when
  the process died is picked up and re-run on the next inbound message (rather
  than the message being dropped).

This is automatic — no flag. What is **not** durable across a restart today: a
session that was mid-LLM-call resumes from its last persisted step, not from the
exact in-flight token position.

The default is one Uvicorn worker (`UVICORN_WORKERS=1`) and that is the supported
shape; running more has two preconditions and a real ceiling, both described in
[deployment-postures.md](deployment-postures.md#the-honest-multi-tenant-ceiling).

---

## Running it as services

Docker is one way; systemd is the other, and it is what a multi-surface deployment
usually wants, because each surface is its own long-running process. Create one
unit per process you need:

| Process | Command | What it is |
|---|---|---|
| Agent (headless) | `polyrob telegram` | The agent itself, long-polling a chat surface. This is the process that runs the autonomy loops. |
| Email surface | `polyrob email` | IMAP poll in, SMTP out. Its own process. |
| API server | `python main.py` (or `polyrob serve`) | REST, A2A and the OpenAI-compatible `/v1` surface. Only needed if you want programmatic access. |
| Console | `python -m uvicorn webview.server:app --host 127.0.0.1 --port 5050 --proxy-headers` | The web console, behind your reverse proxy. See [deployment-postures.md](deployment-postures.md). |
| App supervisor | `polyrob apps supervise` | Only if the agent deploys durable apps. It holds the docker and nginx privilege the agent never has. |

Give every unit the same `EnvironmentFile` (for example `/etc/polyrob/polyrob.env`)
and the same `POLYROB_DATA_DIR`, so they agree about the owner, the data home and
the flags. The console is the one exception: it may take an extra file of its own
for posture and read-only settings.

`polyrob doctor` on the box reports the health of whatever is running — providers,
memory, autonomy state, active pauses — and is the first thing to run when a unit
misbehaves.

### One daemon per profile

If you run several bots, let the CLI write the unit for you:

```bash
polyrob profile create scout --service     # emits polyrob-scout.service
```

The emitted unit sets `POLYROB_PROFILE` and `POLYROB_PROFILES_ROOT` explicitly,
which is what keeps a spawned daemon out of the default home. Each profile needs
its own surface credentials — two daemons long-polling one Telegram token fight
each other. See [profiles.md](profiles.md#daemons).

---

## Updating

In Docker:

```bash
git pull
docker compose up --build
```

Everywhere else, the updater is built in:

```bash
polyrob update --check            # is there a newer release, and what would change
polyrob update --apply            # snapshot, install, migrate, verify, roll back on failure
polyrob update --list-snapshots
polyrob update --rollback         # restore the latest snapshot (data — not the code)
```

`--check` and `--rollback` work on any install. `--apply` performs the update
itself only for a git checkout or an editable install; for a pip, pipx or
system-managed install it prints the exact manual command instead. What the
snapshot covers, and why `--rollback` restores data but not code:
[upgrading.md](upgrading.md#the-safety-net).

---

## Environment flags for server deployments

For multi-user / production deployments, review these defaults:

| Variable | Recommended server value | Notes |
|----------|-------------------------|-------|
| `POLYROB_LOCAL` | *(unset)* | The single-user profile. Leave unset on a server: it is what turns on the interactive tool group (coding, git, knowledge base, project context), and it is also a precondition for the autonomy group |
| `AUTONOMY_ENABLED` | *(unset / `false`)* | Master switch for the self-directed loops (self-wake, goals, curator, writable skills). They need **both** this and `POLYROB_LOCAL`, so a plain server has them off either way |
| `POLYROB_OWNER_USER_ID` | *(set it)* | The owner tenant every surface, the console and `polyrob owner` resolve. Set the same value everywhere |
| `MEMORY_REQUIRE_USER_ID` | `true` (default) | Prevents cross-tenant memory bleed |
| `CODE_EXEC_ENABLED` | `false` (default) | The local subprocess backend is not a sandbox. If you need code execution on a server, use `CODE_EXEC_BACKEND=docker` |
| `CRON_ENABLED` | `false` (default) | Scheduled runs. Off unless you set it, or set `AUTONOMY_POSTURE=full` |
| `SUB_AGENTS_ENABLED` | `true` (default) | Disable to prevent agent delegation |

See [configuration.md](configuration.md) for how these fit together and
[../CONFIGURATION.md](../CONFIGURATION.md) for the complete flag reference.

---

## Letting the agent deploy to Hugging Face Spaces (`hf_deploy`)

The optional `hf_deploy` tool lets the agent publish its own session workspace as
a Hugging Face Space (Docker SDK) — useful for shipping a demo/app it just built.
It is OFF by default and gated at `AGENT_COMPUTE_POSTURE>=2` (self-maintenance
tier); see [../CONFIGURATION.md](../CONFIGURATION.md) for the full flag table.

**1. Provision an HF token.** Create a Hugging Face **fine-grained** access
token scoped to `Write access to contents/settings of all repos under your
personal namespace` (or a specific org), from
https://huggingface.co/settings/tokens. Do not use a `read`-only or org-wide
admin token — a Space-write-scoped token is the least privilege that works.

**2. Set the environment:**

```bash
HF_DEPLOY_ENABLED=true
AGENT_COMPUTE_POSTURE=2          # self-maintenance tier; hf_deploy is refused below this
HF_DEPLOY_ORG=your-hf-username   # or an org you have write access to
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx # the fine-grained write token from step 1
```

**3. First-publish approval vs. approved-app redeploy.** The first `deploy` of a
NEW app name is gated by a real approving provider: the tool resolves the SAME
interactive-default provider the Controller uses at `AGENT_COMPUTE_POSTURE>=2`
(`interactive_cli` unless you set `APPROVAL_PROVIDER` to something else). On an
attended terminal that prompts you to approve; on an **unattended/headless** run
`interactive_cli` cannot prompt and **fail-closes to deny**, so a brand-new
PUBLIC app can never be first-published by an autonomous run — you approve the
first publish yourself, interactively.

Once an app name is approved (its `deployed_apps.db` row records `approved_at`),
later redeploys of that SAME app **skip the approver entirely and run
unattended** — subject only to `HF_DEPLOY_DAILY_MAX` (default 10/day) and
`HF_DEPLOY_MIN_INTERVAL_SEC` (default 120s between deploys of the same app). This
is what lets an autonomous goal iterate on an already-approved app without a
prompt on every deploy. (Note: `deploy` is deliberately NOT in the Controller's
`APPROVAL_REQUIRED_TOOLS` sets — a blanket Controller gate can't tell first
publish from redeploy, so the tool owns that distinction via its registry.)

**4. Ship==tested contract.** Every `deploy` call refuses unless the session's
action ledger shows a green `run_tests` with no code-edit action after it — a
deploy is never the untested state of the workspace.

`HF_TOKEN` is read directly from the process environment at deploy time; it is
never written to a param, a result, or a log line (broker errors are
token-scrubbed). `hf_deploy` is excluded from delegated sub-agents
(`DELEGATE_BLOCKED_TOOLS`) and from correspondent-tainted sessions.

---

## Logs

```bash
# Follow server logs
docker compose logs -f

# Or if running detached
docker compose logs -f polyrob
```

---

## Stopping

```bash
docker compose down
```

Data in `./.polyrob/` is preserved. To also remove the volume, add `-v` (use with caution).
