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

| Posture | Who it's for | Public `/` face | Bind default | Auth |
|---|---|---|---|---|
| `local` (Posture 0) | Single-user, own machine | Full dashboard, no gate | `127.0.0.1` (loopback only) | None — the loopback operator *is* the owner |
| `own_ops` (Posture 1) | You self-host on a public host, for yourself only | Minimal "polyrob is live" status page; console behind login | `0.0.0.0` | Owner username/password login |
| `multitenant` (Posture 2) | SaaS, multiple paying users | Full marketing/SaaS UI, sign-in required | `0.0.0.0` | Wallet/SIWE JWT + admin/billing pages |

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
curator). Goals/cron created from its pages execute only when a worker with
the autonomy runtime is up (`polyrob serve` / `polyrob gateway` / the REPL
under `POLYROB_LOCAL`), not from the dashboard process alone.

### Safe-by-default guarantees

- **`local` never exposes anything** — no login surface is even registered;
  a request to `/owner-login` 404s.
- **`--host 0.0.0.0` auto-derives `own_ops`.** Passing `--host` without an
  explicit `--posture`/`--multitenant` feeds the host into `WEBGATE_HOST`,
  which `webgate.posture()` reads for its host-derivation branch — so a bare
  `polyrob dashboard --host 0.0.0.0` binds publicly *and* requires owner
  login, it never silently binds public-with-no-auth.
- **`own_ops`/`multitenant`, unauthenticated → status page only.** The root
  handler (`webview/server.py::index`) checks `webgate.posture() != "local"
  and not is_authenticated(request)`: if true, it renders the minimal
  `status.html` template ("polyrob is live", instance id, version, uptime)
  and nothing else — the full dashboard/console only renders once a session
  is authenticated (owner login, or in `multitenant`, a wallet/SIWE session).

### Residual: reverse-proxy deploys must set posture explicitly

A deployment that fronts polyrob with a reverse proxy (nginx, Caddy, etc.)
and always binds the app itself to `127.0.0.1` (proxying `0.0.0.0:443` on the
proxy in front of it) **never passes `--host`/`--posture` to the app process**.
In that shape, `webgate.posture()` still derives `local` (loopback bind seen
from inside the process), even though the site is reachable publicly through
the proxy — Posture 0's "no auth, full dashboard" behavior would be exposed
to the internet.

**If you run behind a reverse proxy, set `POLYROB_POSTURE=own_ops` (or
`multitenant`) explicitly in the app's environment** — don't rely on
host-derivation, since the proxy hides the real bind address from
`webgate.posture()`.

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

**"Multi-tenant" in this codebase means single-worker process + tenant-scoped
data — not high-concurrency multi-worker horizontal scaling.** Be precise
about this when deploying `multitenant` for real users:

1. **The live orchestrator object cannot cross process boundaries.** The
   default `SessionRegistry` (`agents/task/session_registry.py`) is a plain
   in-process dict — an orchestrator created in one worker is invisible to
   another. Neither it nor its cross-process-aware sibling
   (`SqliteSessionRegistry`) ever serializes the orchestrator itself, only
   session-id + `worker_pid`/`owner_boot_id` metadata.
2. **`UVICORN_WORKERS=1` is the default, and stays the default.** This is a
   deliberate invariant, not an oversight.
3. **`workers>1` requires BOTH `SESSION_REGISTRY_BACKEND=sqlite` AND
   operator-provided sticky load-balancer routing.** The SQLite registry
   variant gives cross-worker *visibility* (which worker owns a session), not
   transparent cross-worker *method calls* on a remote orchestrator. Sticky
   routing — routing a session's requests to the worker that owns it — is
   infra/load-balancer configuration polyrob does not implement or ship; it
   only makes that routing decision safe to build on top of.
4. **A session request that lands on the wrong worker returns an honest 409,
   not a false 404.** `api/session_routing.py` raises `409` with the owning
   `owner_pid` and a `Retry-After` header when a session is `REMOTE` to the
   worker that received the request — diagnosable, not silently missing.
5. **True cross-worker method forwarding (IPC / a shared serializable
   orchestrator) remains explicitly out of scope.** There is no flag or
   partial implementation of it today.

**Conclusion:** a `multitenant` deployment that needs `workers>1` for
throughput must accept either (a) staying on `UVICORN_WORKERS=1` and scaling
vertically, or (b) opting into `SESSION_REGISTRY_BACKEND=sqlite` plus your
own sticky routing — which buys session-affinity safety (no false-404,
honest 409-with-owner) but **not** actual cross-worker load distribution for
a single live session; a session's turns always execute on the one worker
that created it.

---

## Deploying a public instance (own_ops example)

This is the shape used for a public single-owner instance behind a reverse proxy —
`own_ops` posture.

### 1. Posture and identity

```bash
POLYROB_POSTURE=own_ops          # explicit — do NOT rely on host-derivation behind a proxy
POLYROB_OWNER_USERNAME=youruser
POLYROB_OWNER_PASSWORD_HASH='$argon2id$...'   # from the argon2 one-liner above
JWT_SECRET_KEY=<long random secret>            # required — owner-login mint raises without it
WEBVIEW_DOMAIN=app.example.com                 # SIWE domain + Socket.IO CORS default origin
```

### 2. Bind + reverse proxy

Bind the app to loopback and let nginx (or Caddy) terminate TLS and proxy to
it — this is the standard shape described in `AGENTS.md`'s deployment notes
(`polyrob-api.service` / `polyrob-webgate.service` behind nginx, Let's
Encrypt certs). Because `POLYROB_POSTURE=own_ops` is set explicitly, the app
enforces owner-login even though it's listening on loopback from its own
point of view.

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
  status page (instance id, version, uptime) — **not** the dashboard —
  until you log in.
- `GET https://app.example.com/owner-login` should show the login form.
- After logging in with `POLYROB_OWNER_USERNAME`/password, `/` should show
  the full console.

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
| `POLYROB_OWNER_USERNAME` | unset | Owner login username (`own_ops`/`multitenant`). |
| `POLYROB_OWNER_PASSWORD_HASH` | unset | Argon2 hash of the owner password — never plaintext. |
| `JWT_SECRET_KEY` | unset | Signs both the owner-login cookie and wallet/SIWE JWTs. Required for owner login to work. |
| `WEBVIEW_DOMAIN` | *(the source ships a hardcoded fallback — always set this explicitly for your deployment)* | SIWE domain + default Socket.IO CORS origin. |
| `ENVIRONMENT` | `production` | When `production`, the owner-login cookie is marked `secure` (HTTPS-only). |

See [../CONFIGURATION.md](../CONFIGURATION.md) for the complete flag reference.
