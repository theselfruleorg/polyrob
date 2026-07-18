# POLYROB Webview / Console

_Last reviewed: 2026-07-06. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

Real-time web console for POLYROB: live session monitoring (Socket.IO), a
global activity terminal, session inspection (chat/feed/workspace/stats),
and the webgate v1 pages (Memory / Autonomy / Identity / System).

One ASGI app: `webview/server.py` builds a `socketio.AsyncServer` wrapped
around FastAPI (`app = socketio.ASGIApp(_sio, other_asgi_app=_fastapi)`).
Default port **5050**.

## Postures (SSOT: `webview/webgate.py::posture()`)

| Posture | Who sees what | Auth |
|---|---|---|
| `local` | full dashboard on loopback | none (operator IS the owner) |
| `own_ops` | public `/` shows a status page; console behind owner login | argon2 owner login → JWT `auth_token` cookie |
| `multitenant` | SaaS: SIWE wallet sign-in, shareable session links, admin pages | wallet JWT / owner login |

Set `POLYROB_POSTURE` explicitly behind a reverse proxy. Owner login needs
`POLYROB_OWNER_USERNAME` + `POLYROB_OWNER_PASSWORD_HASH` (argon2) +
`JWT_SECRET_KEY`.

## Pages

- `/` — dashboard (posture-aware; unauthenticated own_ops → status page)
- `/activity` — **global activity terminal**: live stream of everything the
  instance does (all sessions' feed events + goals/cron/telemetry/skill
  events), filterable, with per-session drill-down. Owner/admin-gated in
  every non-local posture.
- `/sessions` — session catalog; `/session/{id}` — Chat / Feed / Workspace /
  Stats tabs + Vision/Info sidebars
- `/memory`, `/autonomy`, `/identity`, `/system` — webgate v1 read-only pages
- `/settings` — MCP servers + skills management
- `/owner-login` (own_ops/multitenant); `/signin`, `/profile`, `/admin*`
  (multitenant only)

## HTTP API (served by this app)

- `GET /api/status` — public instance status
- `GET /api/activity/backfill?limit=N` — recent activity events (gated)
- `GET /api/sessions` — session list (tenant-scoped)
- `GET /api/session/{id}/feed/events?after_seq=&limit=` — feed delta-sync
- `GET /api/session/{id}/{status,stats,agents,services,task,skills,debug}`
- `GET /api/session/{id}/workspace/{tree,file,serve/...}` — traversal-guarded
- `GET /api/session/{id}/screenshot[/file]`
- `POST /api/session/{id}/messages` — owner-only; **403 in read-only mode**;
  proxies to the API service (`:9000`) when present
- `GET /api/repair/{id}` — real telemetry repair (dedup + token estimation);
  403 in read-only mode
- `POST /api/internal/emit` — localhost-only fast push from the telemetry
  service into the session's Socket.IO room
- `GET /api/webgate/{memory,goals,cron,identity,doctor,ledger}` — webgate v1 data
- `GET/PATCH /api/webgate/preferences` — typed prefs over `core.prefs`
  (guarded keys 409 without `confirm:true`; PATCH 403 in read-only mode)
- `GET /api/webgate/pending` + `POST .../pending/{kind}/{id}/{promote,reject}`
  — self-evolution review queue over `core.self_evolution` (decisions 403 in
  read-only mode)
- `GET /api/webgate/knowledge/{notes,episodes,skills,kb,changes}` — the
  `/knowledge` wiki readers

## Socket.IO events (namespace `/`)

Client → server: `join_session {session_id}`, `leave`, `join_activity`,
`leave_activity`.
Server → client: `initial_feed`, `initial_feed_chunk`, `feed_update`,
`stream_chunk`, `activity_snapshot`, `activity_event`, `error`.

`join_session` is tenant-gated (E4); `join_activity` is owner/admin-gated —
the global stream is cross-tenant by nature and never public.

## Activity stream architecture (`webview/activity.py`)

Cross-process by design (the agent may run in a different process, e.g. the
headless VPS): one recursive `watchfiles` watcher over the session data root
(`{user}/{session}/feed/*.json`) plus 2s id-cursor tails over
`telemetry_events.db`, `goals.db::goal_events`, and
`skill_usage.db::skill_install_audit`, all normalized into
`{id, ts, source, user_id, session_id, kind, summary, payload}` and emitted
to the `activity` room. The hub starts on the first watcher and stops when
the room empties. Flags: `WEBVIEW_ACTIVITY_ENABLED` (default on),
`WEBVIEW_ACTIVITY_TAIL_SEC` (default 2.0).

## Read-only mode

`WEBVIEW_READ_ONLY=true` turns the console into a pure monitoring surface:
mutating endpoints return 403 server-side and the chat input is not
rendered. Use this on deployments where the agent is driven elsewhere
(Telegram, CLI) and the console only observes.

## Security

- Owner login: argon2, constant-time, per-IP throttle (5/5min), stateless
  double-submit CSRF, sanitized `return_to`, JWT cookie (`httponly`,
  `samesite=lax`, `secure` in production).
- Socket rooms gated per tenant/owner; IP + per-session event rate limits.
- Security headers middleware (CSP, X-Frame-Options DENY, nosniff);
  workspace file serving is path-traversal-guarded.
- Do not regress the 2026-07-03 console fixes (E4/B4/B7/E2/E5/H2b) or the
  2026-07-06 hardening (throttle/CSRF/read-only/emit-room) — all covered by
  `tests/unit/webview/`.

## Running

Local dev: `python -m uvicorn webview.server:app --port 5050` (or
`webview/server_launcher.py`). Tests: `pytest tests/unit/webview -q`.

## Deployment shapes

- **Standalone monitoring console next to the headless agent** (e.g. the Rob
  prod VPS): `deployment/polyrob-webview.service` (loopback bind, env from
  `/etc/polyrob/polyrob.env` + `/etc/polyrob/webview.env`) behind
  `deployment/nginx-webview-ownops.conf`. Deploy with
  `scripts/deploy_webview.sh` (backup → rsync → unit+vhost → verify).
- **Classic api+webgate shape**: `deployment/polyrob-webgate.service`
  alongside `polyrob-api.service` behind `deployment/nginx.conf` — see
  `deployment/README.md` and `docs/guide/deployment-postures.md`.
