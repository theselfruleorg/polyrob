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

⚠️ **Copy `config/.env.example` for Docker.** It is the container-shaped template:
`POLYROB_DATA_DIR=/app/.polyrob`, which is where the compose volume is mounted. The
file named `config/.env.development.template` is for a **host** install — it sets
`POLYROB_DATA_DIR=/var/lib/polyrob`, and copying it into a container puts your data
outside the volume, so nothing survives a restart. (`config/.env.production.example`
is the same shape for a host production install.)

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

⚠️ **The image is API-only.** `.[server,browser,memory-vector]` does not carry the
`telegram`, `crypto`, `solana`, `twitter` or `voice` extras, and the entrypoint is
`python main.py` (the FastAPI app). So a compose deployment gives you REST, A2A and
the OpenAI-compatible `/v1` surface, plus the browser and vector-memory extras — it
does **not** run `polyrob telegram`, the email surface, or any money verb. For those,
use a host install: `pip install -c requirements.lock ".[server,browser,crypto,solana,telegram,twitter,voice]"`
from the repo root (that is the extras set the production deployer installs — see
[../../DEPLOYMENT.md](../../DEPLOYMENT.md)), or add the extras to your own image.

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
| Console | `python -m uvicorn webview.server:app --host 127.0.0.1 --port 5050 --proxy-headers --forwarded-allow-ips=127.0.0.1` | The web console, behind your reverse proxy. **Requires `POLYROB_POSTURE=own_ops` plus `POLYROB_OWNER_USERNAME`, `POLYROB_OWNER_PASSWORD_HASH` and `JWT_SECRET_KEY`** — see the warning below and [deployment-postures.md](deployment-postures.md). |
| App supervisor | `polyrob apps supervise` | Only if the agent deploys durable apps. It holds the docker and nginx privilege the agent never has. |
| Isolated browser | `polyrob browser install` (writes `polyrob-browser.service`) | Required as soon as the wallet is enabled. A custody process never launches Chromium beside the signer; it connects to this one. See below. |

Give every unit the same `EnvironmentFile` (for example `/etc/polyrob/polyrob.env`)
and the same `POLYROB_DATA_DIR`, so they agree about the owner, the data home and
the flags. The console is the one exception: it takes an extra file of its own
(`/etc/polyrob/webview.env`, `chmod 600`, outside the code tree) for posture, owner
credentials and read-only settings, as the shipped console unit does.

> ⚠️ **The console REFUSES to start as an anonymous server.** At the default `local`
> posture there is no login at all — every anonymous request is treated as the owner,
> with the whole control plane behind it. `webview/posture_guard.py` therefore looks
> for the signals the posture resolver cannot see (`--proxy-headers` or
> `--forwarded-allow-ips` on the argv, a `POLYROB_DATA_DIR` outside your home,
> `WEBVIEW_PUBLIC_URL`) and aborts the boot with `REFUSING TO START`. A served
> console sets:
>
> ```ini
> # /etc/polyrob/webview.env
> POLYROB_POSTURE=own_ops
> POLYROB_OWNER_USERNAME=youruser
> POLYROB_OWNER_PASSWORD_HASH='$argon2id$v=19$...'   # never a plaintext password
> JWT_SECRET_KEY=<a long random secret>
> ```
>
> The escape hatch `WEBVIEW_ALLOW_LOCAL_POSTURE=1` exists for an operator who fronts
> the console with their own auth layer; it is honoured with a loud warning, never
> silently. Full walkthrough, including the argon2 one-liner:
> [deployment-postures.md](deployment-postures.md).
>
> ⚠️ Never pass `--forwarded-allow-ips=*`. Name the proxy's address (`127.0.0.1` for
> nginx on the same box), or uvicorn believes any client's `X-Forwarded-For` and a
> direct caller can claim to be localhost. `polyrob serve` defaults to `127.0.0.1`
> and reads `UVICORN_FORWARDED_ALLOW_IPS` when you genuinely need another value.

`polyrob doctor` on the box reports the health of whatever is running — providers,
memory, autonomy state, active pauses — and is the first thing to run when a unit
misbehaves.

### The isolated browser (required with a wallet)

When `AGENT_WALLET_ENABLED=true` or a master seed is present, the agent process
is a **custody** process and refuses to launch Chromium: a browser renders
untrusted pages, and it must not run as the same OS user as the signer. The
browser tool, the X browser rail (`x_browser`) and web dapps (`dapp_connect`)
all need a browser, so a custody deployment runs one as its own principal:

```bash
sudo polyrob browser install          # once, as root (add --with-deps on a fresh box)
echo 'BROWSER_CDP_URL=http://127.0.0.1:9222' | sudo tee -a /etc/polyrob/polyrob.env
sudo systemctl restart polyrob.service
polyrob browser status                 # the rail line + the host facts
```

What `install` sets up, and why each part matters:

- a dedicated system user `polyrob-browser` with a systemd-hardened unit — no
  custody environment, no data home, loopback-only CDP listener;
- the **Chromium sandbox ON**. Ubuntu 24.04 restricts unprivileged user
  namespaces through AppArmor, so the installer writes a six-line profile
  (`/etc/apparmor.d/polyrob-browser`, Ubuntu's own `chrome` template with the
  path changed) that lifts the restriction for this binary only. The unit never
  carries `--no-sandbox`;
- an **egress chain** keyed on the browser's UID (`polyrob-browser-egress.service`):
  the browser cannot open connections to loopback services (the console, its
  own CDP port), RFC1918, link-local or the cloud metadata service. The agent's
  Playwright route guard is the second line; this is the first;
- Chromium for the Playwright version in the agent's venv, under
  `/opt/polyrob-browser`. `polyrob browser update` re-installs it for the
  current pin; `polyrob browser status` prints the installed revision beside
  the pin and names drift.

Every session gets a fresh browser context over CDP (its own cookies, its own
login), so nothing the agent does lands in the service's persistent profile.
CDP has no authentication by design: any local user can drive the browser, which
is acceptable on a single-owner box because no credential persists in it.

**A second host (removes the shared kernel).** The same-host service is a
boundary of UID + sandbox + egress; what it cannot remove is the kernel it
shares with the signer. To remove that too, run the browser on its own small
box on the same private network (or over WireGuard):

```bash
# on the BROWSER box (no wallet, no data home, no LLM keys — only the [browser] extra)
pip install 'polyrob[browser]==<the agent's version>'
sudo polyrob browser install --mode server --listen <this box's private IP> --with-deps
#   → prints BROWSER_WSS_URL=ws://<private-ip>:3000/<token>

# on the AGENT box
echo 'BROWSER_WSS_URL=ws://<private-ip>:3000/<token>' | sudo tee -a /etc/polyrob/polyrob.env
sudo systemctl restart polyrob.service
```

`--mode server` writes the same user, AppArmor profile and egress chain, but the
unit runs a Playwright server (`python -m playwright run-server`) bound to the
private address, with the token kept in `/etc/polyrob/browser-server.env`
(root, `0600`) — the URL path is the only authentication the protocol has, so
allow inbound TCP 3000 from the agent's private IP only and never bind a public
interface (the installer refuses `0.0.0.0`). The Playwright protocol requires the
**same Playwright version on both ends**; the installer prints the version it
serves. Prefer this over CDP across a network: CDP has no authentication at all.

**Moving an X login onto the box.** `polyrob x-account capture-session` is a
desktop ceremony (it opens a visible browser and refuses on a custody host).
The captured session lives in `<data_home>/.x_session.json`, encrypted with
`MCP_ENCRYPTION_KEY` and keyed by the instance identity — both per-box, so that
file is NOT portable. Hand it over as plain Playwright storage state instead:

```sh
# on the desktop (visible browser), sign in as the agent's account:
polyrob x-account capture-session --out x-session.json
scp x-session.json server:/tmp/
# on the server, as the agent identity, stored under the server's own key:
sudo -u polyrob-agent -H env POLYROB_DATA_DIR=/var/lib/polyrob \
  /opt/polyrob/venv/bin/polyrob x-account import-session /tmp/x-session.json --handle <handle>
rm /tmp/x-session.json   # it is the login in plain text
```

No desktop install? `import-session --auth-token <v> --ct0 <v>` takes the two
login cookies straight from a signed-in browser (DevTools → Application →
Cookies → x.com). Either way `polyrob x-account status` shows the stored
handle and the agent's `x_login_check` verifies it live.

`polyrob doctor`, `/status` and the agent's own tool catalog all report the rail
in one of three states: `none (custody)` with the install remedy, `configured,
unreachable (<reason>)` with the service remedy, or `remote cdp ok (<version>)`.

### One daemon per profile

If you run several bots, let the CLI write the unit for you:

```bash
polyrob profile create scout --service     # writes /etc/systemd/system/polyrob@.service
sudo systemctl enable --now polyrob@scout
```

The unit is a systemd **template** (`polyrob@.service`, `%i` = the profile name), so
one file serves every profile and `polyrob@scout` can never collide with the
`polyrob-email` / `polyrob-webview` sibling units. It sets `POLYROB_PROFILE` and
`POLYROB_PROFILES_ROOT` explicitly, which is what keeps a spawned daemon out of the
default home — so the profile must live under the SAME root the unit names
(`POLYROB_PROFILES_ROOT=/var/lib/polyrob/profiles polyrob profile create <name>` for a
server layout; the CLI default is `~/.polyrob/profiles`). Without write access to
`/etc/systemd/system` the command writes the rendered unit into the profile home and
prints the `cp` to run. Each profile needs its own surface credentials — two daemons
long-polling one Telegram token fight each other. See
[profiles.md](profiles.md#daemons).

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

`--check` works on any install. `--apply` performs the update itself **only** for a
git checkout or an editable git install; for a pip, pipx, systemd or Docker install it
prints the exact manual command for that method and exits non-zero — it never pretends
to have updated anything. `--rollback` needs a snapshot to exist, and snapshots are
created by `--apply` and by the boot-time migration, so an install that has only ever
updated through its package manager has nothing to roll back to yet. What the snapshot
covers, and why `--rollback` restores data but not code:
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
