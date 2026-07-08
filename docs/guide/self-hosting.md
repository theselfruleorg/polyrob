# Self-Hosting

Run polyrob on your own server using Docker Compose. This is the recommended path for a persistent, always-on deployment.

> For the **web console** (`polyrob dashboard`) specifically — which posture to run it in,
> owner login, and the honest multi-tenant ceiling — see
> [deployment-postures.md](deployment-postures.md).

---

## Prerequisites

- Docker ≥ 24 and Docker Compose V2
- At least one LLM provider API key (see [configuration.md](configuration.md))

---

## Quick start

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

### Running with more than one worker

The default is a single Uvicorn worker (`UVICORN_WORKERS=1`), which is safe
because the live orchestrator object cannot cross processes. To run `workers>1`
you need **both**:

1. `SESSION_REGISTRY_BACKEND=sqlite` — mirrors session→owner-worker metadata
   cross-process, so a worker that does not own a session returns an honest
   **409 + `owner_pid` + `Retry-After`** instead of a false 404.
2. **Sticky load-balancer routing** — route each session to the worker that owns
   it (e.g. hash the session id at the proxy).

Without sticky routing, a request can land on a worker that does not hold the
live session and gets a 409. True cross-worker method forwarding is out of scope;
for most deployments, one worker plus vertical scaling is simpler than sticky
multi-worker. See [deployment-postures.md](deployment-postures.md) for the full
recipe and rationale.

---

## Updating

```bash
git pull
docker compose up --build
```

The `--build` flag forces a rebuild of the image with the latest code.

---

## Environment flags for server deployments

For multi-user / production deployments, review these defaults:

| Variable | Recommended server value | Notes |
|----------|-------------------------|-------|
| `POLYROB_LOCAL` | *(unset)* | Leave unset; this keeps autonomy flags OFF by default |
| `MEMORY_REQUIRE_USER_ID` | `true` (default) | Prevents cross-tenant memory bleed |
| `CODE_EXEC_ENABLED` | `false` (default) | Local subprocess exec is not sandboxed — keep off until a hard-sandbox backend is added |
| `CRON_ENABLED` | `false` (default) | Enable if you want scheduled tasks |
| `SUB_AGENTS_ENABLED` | `true` (default) | Disable to prevent agent delegation |

See [../CONFIGURATION.md](../CONFIGURATION.md) for the complete flag reference.

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
