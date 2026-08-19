# `config/.env.*` — legacy server-mode env tier

_Last reviewed: 2026-08-14 (env-scheme unification)._

**DO NOT commit real `.env` files to git.** They hold API keys and secrets.
The tracked files here are templates/examples only.

## What this directory is FOR

`config/.env.{development,production}` is the **server-mode non-systemd** env
tier — the OSS self-hosting posture where `polyrob serve` / `python main.py`
runs on a box with no unit-file env management. Nothing else needs it:

- **Systemd deploys (Rob #1 prod)** read `/etc/polyrob/polyrob.env` via the
  unit file. They never read this directory.
- **The local CLI** keeps these files only as the LOWEST-precedence
  back-compat layers. CLI keys belong in `~/.polyrob/.env` — copy them once
  with `polyrob config migrate`. (The automatic key backfill from this
  directory is retired; see `POLYROB_ENV_KEY_BACKFILL` in
  `docs/CONFIGURATION.md`.)

## Environment names

Exactly two names are meaningful: `development` (default) and `production`.
There is **no staging environment**. The active name resolves
`CONFIG_ENV` > `ENV` > `development` (`core/bootstrap.py::load_env`).

## Layering (SSOT: `core.paths.env_file_candidates`)

- Server mode: `config/.env.{env}.local` > `config/.env.{env}` > root `.env`,
  loaded with `override=True` (files win over process env).
- Local/CLI mode: process env > `./.polyrob/.env` > `~/.polyrob/.env` >
  legacy `~/.rob/.env` > root `.env` > this directory's files (lowest).

Inspect the live view with `polyrob config path`; `polyrob doctor` names the
source tier on every credential line.

## Flag reference

Every flag and its default lives in `docs/CONFIGURATION.md` (the SSOT) —
this file no longer duplicates that table. Runtime view:
`polyrob doctor --flags`.

## Files here

- `.env.example`, `.env.development.template`, `.env.production.example` —
  tracked templates for a fresh server-mode deploy.
- `.env.production` (untracked, 0600 when present) — a pre-CLI relic on dev
  checkouts; run `polyrob config migrate`, then delete it.
- `mcp_config.json` — MCP server config (`${VAR}` placeholders; see
  `tools/mcp/README.md`).
