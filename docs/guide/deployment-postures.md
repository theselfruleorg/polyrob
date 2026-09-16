# Deployment postures

polyrob's web console (`webview/`) runs in one of three **postures** — `local`,
`own_ops`, or `multitenant`. The posture decides what the public `/` page shows,
whether a login is required, and what bind address is safe to use. This page
documents the posture model **as built**; the code is the source of truth
(`webview/webgate.py`, `webview/owner_auth.py`, `cli/commands/dashboard.py`).

> This page covers the *web console*. For environment variables in general see
> [configuration.md](configuration.md) and the full flag reference
> [../CONFIGURATION.md](../CONFIGURATION.md).

---

## The three postures

| Posture | Who it's for | What `/` shows a visitor | Bind default | Auth |
|---|---|---|---|---|
| `local` (Posture 0) | Single-user, own machine | The console itself, no gate | `127.0.0.1` (loopback only) | None — the loopback operator *is* the owner |
| `own_ops` (Posture 1) | You self-host on a public host, for yourself only | A minimal "polyrob is live" status page; the console appears after login | `0.0.0.0` | Owner username/password login |
| `multitenant` (Posture 2) | Several tenants with their own accounts | The same status page until sign-in; account and admin pages exist only here | `0.0.0.0` | Wallet/SIWE JWT + admin pages |

The primitive is `local`: loopback bind, zero auth, every session owned by
the local user. `own_ops` and `multitenant` are layers on top, gated by
posture — they don't replace the local behavior, they add to it.

### How posture is resolved

`webgate.posture()` (`webview/webgate.py`) resolves in this order:

1. **Explicit `POLYROB_POSTURE`** (`local` | `own_ops` | `multitenant`, case-insensitive) — wins outright.
2. **`WEBGATE_MULTITENANT=true`** (back-compat alias) → `multitenant`.
3. **Derived from an explicit `WEBGATE_HOST` / `WEBVIEW_HOST` override**: a loopback
   address (`127.0.0.1`, `localhost`, `::1`) → `local`; anything else → `own_ops`.
4. **No override, `WEBGATE_MULTITENANT` not set** → `local` (today's default:
   loopback, no auth — this must never regress).

`webgate.bind_host()` mirrors that logic for the actual bind: `local` → `127.0.0.1`,
`own_ops`/`multitenant` → `0.0.0.0`, unless `WEBGATE_HOST`/`WEBVIEW_HOST` is set
explicitly (which always wins).

### Running it

```bash
# Posture 0 — local (default): loopback, no auth
polyrob dashboard

# Posture 1 — own_ops: public status page + owner login for the console
polyrob dashboard --posture own_ops
# or, if binding non-loopback directly:
polyrob dashboard --host 0.0.0.0

# Posture 2 — multitenant: full JWT/SIWE + admin/billing layer
polyrob dashboard --multitenant
```

`polyrob dashboard` is also aliased as `polyrob webgate`. It launches a
viewer + chat UI only — it does **not** run the autonomy loops (cron/goals/
curator). Goals/cron created from its pages execute only when a worker with the
autonomy runtime is up **and the relevant loop is enabled** (`polyrob serve` /
`polyrob gateway`, or the REPL under `POLYROB_LOCAL` + `AUTONOMY_ENABLED` — autonomy
is OFF by default), not from the Console process alone.

### Safe-by-default guarantees

- **`local` never exposes anything** — no login surface is even registered;
  a request to `/owner-login` 404s.
- **`--host 0.0.0.0` auto-derives `own_ops`.** Passing `--host` without an
  explicit `--posture`/`--multitenant` feeds the host into `WEBGATE_HOST`,
  which `webgate.posture()` reads for its host-derivation branch — so a bare
  `polyrob dashboard --host 0.0.0.0` binds publicly *and* requires owner
  login, it never silently binds public-with-no-auth.
- **`own_ops`/`multitenant`, unauthenticated → status page only.** On those two
  postures an unauthenticated request to `/` gets the minimal status page
  ("polyrob is live", instance id, version) and nothing else. The console renders
  only once the session is authenticated.
- **A page you may not see is absent, not denied.** Posture decides which routes
  are registered at all, so an account or admin page outside its posture answers
  404 rather than an access-denied screen. Inside `multitenant`, a signed-in
  non-admin who follows an admin link is redirected to `/`.

### Behind a reverse proxy, set the posture explicitly

A deployment that fronts polyrob with a reverse proxy (nginx, Caddy) binds the app
itself to `127.0.0.1`, so posture derivation sees loopback and would answer
`local` — no auth — for a site the internet can reach. **Set
`POLYROB_POSTURE=own_ops` (or `multitenant`) in the app's environment** and do not
rely on host-derivation.

The console does not leave that to trust. At `local` posture it **refuses to
start** when the deployment looks like a server, and names what it saw:

- `WEBVIEW_PUBLIC_URL` is set — the console has a public address;
- the process runs with `--proxy-headers` / `--forwarded-allow-ips`;
- `POLYROB_DATA_DIR` points outside your home directory (a service tree such as
  `/var/lib/polyrob`).

The refusal aborts the boot with the remedy in the message. If you genuinely
front the console with your own authentication layer, set
`WEBVIEW_ALLOW_LOCAL_POSTURE=1`; it is honoured with a warning, never silently.
A profile's `~/.polyrob/profiles/<name>/data` is deliberately not a signal, so
`polyrob -P <name> dashboard` on a laptop still works.

### A writable console has two preconditions

`WEBVIEW_READ_ONLY=true` makes the console a monitor; leaving it unset makes it a
control plane. A **non-`local`** console that can write refuses to boot until both
of these hold, and it reports every unmet one at once rather than one per restart:

1. **A bound owner** — `POLYROB_OWNER_USER_ID`, set to the same value the agent
   service uses. Without it the console scopes every read *and write* to the
   fallback tenant and renders honest-looking empty lists over the owner's real
   goals, invoices and pending items.
2. **`SESSION_REGISTRY_BACKEND=sqlite` on both units** — the console carries its
   own agent, so with the default in-process registry a message sent from here to
   a live session would resume it in this process: two processes stepping one
   session, one workspace. With the shared registry the session resolves as remote
   and the console answers an honest 409 naming the owning process instead.

On a workstation (`local` posture with no server signal) both are warnings rather
than refusals — one owner, one tree, usually one process.

---

## Owner login (`own_ops`)

A public `own_ops` host requires an authenticated identity to reach the
console — but that identity does **not** need to be a crypto wallet. Managing
your own agent only needs a username/password.

### Configure it

```bash
# 1. Generate an argon2 password hash (never store plaintext)
python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('your-password-here'))"

# 2. Set the env vars
POLYROB_OWNER_USERNAME=youruser
POLYROB_OWNER_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$...'
JWT_SECRET_KEY=<a long random secret>
```

`webview/owner_auth.py::owner_credentials_configured()` requires **both**
`POLYROB_OWNER_USERNAME` and `POLYROB_OWNER_PASSWORD_HASH` to be set; without
them, owner login always fails closed. `verify_owner_password()` is
constant-time by construction — it runs exactly one argon2 verify per attempt
(against the real hash if the username matches, otherwise a precomputed dummy
hash) and compares the username with `hmac.compare_digest`, so a bad
username never returns faster than a bad password. This prevents both
user-enumeration and username/password timing oracles.

### What happens on success

`POST /owner-login` verifies the submitted credentials and, on success, mints
a session cookie (`issue_owner_session_cookie`) — a JWT signed with
`JWT_SECRET_KEY`, 7-day expiry, `httponly`/`samesite=lax`, `secure` when
`ENVIRONMENT=production`. It carries `role=owner`, `tier=admin`,
`payment_method=None`. This is the **same cookie name, algorithm, and
`request.state` contract** the wallet-JWT path uses (`api/auth_state.py`), so
the rest of the app (session ownership, socket auth, etc.) treats an owner
login identically to any other authenticated session — no separate code
path to maintain.

`/owner-login` is registered for both `own_ops` and `multitenant` (not
`multitenant`-only) — in `multitenant`, wallet sign-in (`/signin`) is still
available too; owner login is just one more way in. In `local`, neither login
route is registered at all.

### Wallet sign-in stays optional

Crypto wallet / SIWE sign-in (`/signin`) is only meaningful in `multitenant`
(where paying tenants need their own identity). `own_ops` never requires a
wallet — the owner-login path above is sufficient to run and manage your own
instance.

---

## The honest multi-tenant ceiling

**"Multi-tenant" here means one worker process with tenant-scoped data — not
horizontal scaling.** Be precise about this before you put paying users on it.

`UVICORN_WORKERS=1` is the default and stays the default, because a live session's
orchestrator is an in-process object: it never crosses a process boundary, and no
registry ever serializes it.

To run `workers>1` you need **both**:

1. `SESSION_REGISTRY_BACKEND=sqlite`, which mirrors *which worker owns which
   session* across processes. A request for a session another worker holds then
   gets an honest **409** naming the owning process id, with a `Retry-After` —
   never a false 404.
2. **Sticky load-balancer routing** — send a session's requests to the worker that
   owns it. That is your proxy's configuration; polyrob does not ship it, it only
   makes the routing decision safe to build on.

Even then, a single session's turns always execute on the one worker that created
it. Multiple workers buy you concurrent *sessions*, not a faster session. True
cross-worker forwarding is out of scope, and there is no partial implementation of
it to discover. For most deployments one worker plus a bigger box is simpler.

---

## Deploying a public instance (own_ops example)

This is the shape used for a public single-owner instance behind a reverse proxy —
`own_ops` posture.

### 1. Posture and identity

```bash
POLYROB_POSTURE=own_ops          # explicit — do NOT rely on host-derivation behind a proxy
POLYROB_OWNER_USERNAME=youruser
POLYROB_OWNER_PASSWORD_HASH='$argon2id$...'   # from the argon2 one-liner above
POLYROB_OWNER_USER_ID=<your owner tenant>      # the same value the agent service uses
JWT_SECRET_KEY=<long random secret>            # required — owner-login mint raises without it
WEBVIEW_DOMAIN=app.example.com                 # SIWE domain + Socket.IO CORS default origin
WEBVIEW_READ_ONLY=true                         # drop this only after reading the write preconditions above
```

### 2. Bind + reverse proxy

Bind the app to loopback and let nginx (or Caddy) terminate TLS and proxy to it.
Because `POLYROB_POSTURE=own_ops` is set explicitly, the app enforces owner login
even though it is listening on loopback from its own point of view.

```nginx
server {
    listen 443 ssl;
    server_name app.example.com;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 3. Verify

- `GET https://app.example.com/` should show the minimal "polyrob is live"
  status page (instance id, version, uptime) — **not** the Console —
  until you log in.
- `GET https://app.example.com/owner-login` should show the login form.
- After logging in with `POLYROB_OWNER_USERNAME`/password, `/` should show
  the full console.

### Two deployment shapes

The console is a separate process from the agent, so there are two shapes and you
pick one:

- **Console beside a headless agent.** The agent runs a chat surface
  (`polyrob telegram`, `polyrob email`) and the console only watches it. Run the
  console as its own service with `WEBVIEW_READ_ONLY=true`; the owner still logs
  in to look, and nothing the console can do changes agent state. This is the
  shape the project's own production instance runs.
- **Console beside the API.** `polyrob serve` (the FastAPI app) plus the console,
  both behind one reverse proxy. Use this when you also want the REST, A2A and
  OpenAI-compatible surfaces — see [api.md](api.md).

Either way the console answers on `:5050` by default. `polyrob dashboard` runs it
in the foreground; [self-hosting.md](self-hosting.md) covers running it as a
service.

### Zero-exposure alternative: VPN / Tailscale tunnel

If you don't want the console reachable from the public internet at all —
even behind owner-login — run it as `local` (loopback-only, the default) and
reach it through a private tunnel instead of opening a port:

```bash
polyrob dashboard          # Posture 0: binds 127.0.0.1:5050, no auth

# On a machine joined to the same Tailscale network:
tailscale serve https / http://127.0.0.1:5050
# or, for access from any device on your tailnet without a public hostname:
ssh -L 5050:localhost:5050 you@your-host
```

This keeps the app itself in Posture 0 (no login code path exposed at all)
while still letting you reach it from another device — the console never
listens on a public interface, and traffic never leaves your private
network/tunnel. Prefer this over `own_ops` when the only person who ever
needs console access is you and you already have a VPN/tailnet set up.

---

## Reference: environment variables

| Variable | Default | Meaning |
|---|---|---|
| `POLYROB_POSTURE` | unset (derived) | `local` \| `own_ops` \| `multitenant`. Explicit value always wins. |
| `WEBGATE_MULTITENANT` | `false` | Back-compat alias for `POLYROB_POSTURE=multitenant`. |
| `WEBGATE_HOST` / `WEBVIEW_HOST` | unset | Explicit bind host override; also feeds posture derivation when `POLYROB_POSTURE` is unset. |
| `WEBGATE_PORT` / `WEBVIEW_PORT` | `5050` | Bind port. |
| `WEBVIEW_ALLOW_LOCAL_POSTURE` | off | Override the `local`-posture-on-a-server boot refusal. Only if you front the console with your own auth. |
| `WEBVIEW_READ_ONLY` | `false` | Monitoring-only console: every mutating endpoint returns 403 and the chat input is not rendered. |
| `POLYROB_CONSOLE_NAME` | `POLYROB Console` | The console's own display name. Naming the instance does not rename the console. |
| `POLYROB_OWNER_USERNAME` | unset | Owner login username (`own_ops`/`multitenant`). |
| `POLYROB_OWNER_PASSWORD_HASH` | unset | Argon2 hash of the owner password — never plaintext. |
| `POLYROB_OWNER_USER_ID` | unset | The owner tenant. Required for a writable non-`local` console. |
| `JWT_SECRET_KEY` | unset | Signs both the owner-login cookie and wallet/SIWE JWTs. Required for owner login to work. |
| `WEBVIEW_DOMAIN` | *(the source ships a hardcoded fallback — always set this explicitly for your deployment)* | SIWE domain + default Socket.IO CORS origin. |
| `ENVIRONMENT` | `production` | When `production`, the owner-login cookie is marked `secure` (HTTPS-only). |

The complete reference is [../CONFIGURATION.md](../CONFIGURATION.md); on a running
box, `polyrob doctor --flags --search WEBVIEW` prints the resolved values and where
each one came from.

## Durable apps

The agent can run a built app as its own hardened container behind
`https://<slug>.<APP_SERVICE_BASE_DOMAIN>`, surviving session end and restarts. The
agent only writes a registry row; a supervisor you own does every privileged step.
Turn the agent's half on with one setting:

```
AGENT_BUILDER_MODE=ship            # build = static publish + github only; off = nothing
APP_SERVICE_BASE_DOMAIN=apps.example.com
```

`ship` clamps back to `build` (with a warning) until the base domain is set and its
wildcard certificate exists. The serving side is a one-time owner setup on the box:

1. Obtain a `*.apps.example.com` certificate (DNS-01 — one TXT record).
2. Point an nginx server block for that wildcard at an include directory the
   supervisor writes per-app stanzas into.
3. Run `polyrob apps supervise` as a service under an account that may use docker
   and reload nginx. The agent never has that privilege.

The **first** deploy of each new slug waits for you — `polyrob apps approve <slug>`
or Telegram `/apps approve <slug>`. The default console has no separate Apps page;
use its Inbox or the `/apps` owner verb ([console.md](console.md#what-you-can-do-here)).
An approved address
then redeploys unattended within `APP_SERVICE_MAX_LIVE` / `APP_SERVICE_DAILY_MAX` /
`APP_SERVICE_MIN_INTERVAL_SEC`. `/pause apps` (or `/halt`) stops new deploys **and**
tears down live containers until you resume. Egress is deny-by-default per app, and
no host secret ever reaches an app container. Flags:
[../CONFIGURATION.md](../CONFIGURATION.md).
