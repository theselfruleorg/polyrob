# `config/` — env-file templates

**DO NOT commit real `.env` files to git.** They hold API keys and secrets.
The tracked files here are templates/examples only.

## Nothing loads `config/.env.*`

No polyrob process reads a `config/.env.*` file. Every entry point — the CLI,
the REPL, `polyrob serve` / `python main.py`, the surfaces and the systemd
units — reads ONE env ladder (SSOT: `core.paths.env_file_candidates`), loaded
first-wins, so the process environment beats every file:

1. the process environment (a shell export, a systemd `EnvironmentFile` such as
   `/etc/polyrob/polyrob.env`, or a container's `env_file`),
2. on a box with a deployed instance, the instance file `<data home>/.env`,
3. `~/.polyrob/.env` (the active config home, written by `polyrob config set`),
4. the legacy `~/.rob/.env` (read-only fallback).

`polyrob config path` prints the live view and names a leftover
`config/.env.production` / `config/.env.development` as "NOT read";
`polyrob config migrate` copies its keys into `~/.polyrob/.env`.
`polyrob doctor` names the source tier on every credential line.

## Environment names

Exactly two names are meaningful: `development` (default) and `production`.
There is **no staging environment**. The active name resolves
`CONFIG_ENV` > `ENV` > `development` (`polyrob/app/bootstrap.py::load_env`).
The name selects no file.

## Flag reference

Every flag and its default lives in `docs/CONFIGURATION.md` (the SSOT) —
this file no longer duplicates that table. Runtime view:
`polyrob doctor --flags`.

## Files here

- `.env.example` — the container template: `docker-compose.yml` passes it to the
  container as `env_file` (`POLYROB_DATA_DIR=/app/.polyrob`, inside the volume).
  Copy it to an untracked file for real secrets and point `env_file` at the copy.
- `.env.development.template`, `.env.production.example` — host-install
  templates (`POLYROB_DATA_DIR=/var/lib/polyrob`). Copy one to the file your
  process reads: a systemd unit's `EnvironmentFile` (for example
  `/etc/polyrob/polyrob.env`, mode 0600) or `~/.polyrob/.env`.
- `mcp_config.json` — MCP server config (`${VAR}` placeholders; see
  `tools/mcp/README.md`).
