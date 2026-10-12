# Operate the service — start, stop, restart, update

POLYROB runs in the background as **one service**: `polyrob service install` writes a
systemd user unit (Linux) or a launchd agent (macOS) that runs `polyrob gateway` — every
chat surface you enabled plus the background loops (cron, goals, curator).

This page is how you keep that service current: restart it after a setting changes,
update it to a new release, and do both from a shell **or from your chat**.

## The short version

| You want to… | From a shell | From your chat |
|---|---|---|
| See what runs | `polyrob service status` | `/status` |
| Restart (finish the current work first) | `polyrob service restart` | `/restart` |
| Restart at once | `polyrob service restart --now` | `/restart now` |
| Update to the latest release | `polyrob update --apply` | `/update` |
| Stop / start | `polyrob service stop` / `start` | — |
| Read the log | `polyrob service logs -f` | — |

## What a restart does

A restart never cuts you off in the middle of a reply:

1. **The service stops starting new background work** (no new cron job, no new goal
   run) but keeps taking your messages.
2. **It waits until it is idle** — no reply being written, no goal or cron job running —
   up to 10 minutes (`--wait-idle SECONDS` on the CLI).
3. **It stops taking new messages and exits.** Messages you send during the restart are
   not lost: Telegram keeps them and they are answered when the service is back.
4. **The service manager starts it again** (the unit restarts the process on exit code
   75 — `RestartForceExitStatus=75`).
5. **It tells you it is back** — in the chat you asked from:
   `✅ Back online on 1.4.0 (abc1234) — restart took 41 s.`

If the service is still busy when the wait ends, **nothing restarts** and you are told
what was busy. Ask again later, or use `/restart now` / `--now` to cut the work.

`/restart` and `/update` show a quote with a **Confirm** button first; nothing happens
until you confirm. Both are owner-only and refused inside a group room.

## What an update does

`/update` (or `polyrob update --apply` while the service runs) is a restart with one more
step: **the update is applied by the new process before it starts anything** —

    finish current work → exit → snapshot → install → migrate → verify → start the new version

- Any failing step rolls the code **and** the data back to the snapshot, and the old
  version starts.
- If the new version installs but **does not start** (three starts in a row without a
  healthy start), it is rolled back the same way.
- Either way you get one message with the outcome:
  `✅ Updated and back online on 1.4.0 (was 1.3.0)` or
  `⚠️ The update failed at the 'migrate' step: … It was rolled back. Back online on 1.3.0.`

Automated updates are for **git and editable installs** (what `install.sh` sets up). A
pip, pipx or Docker install tells you the manager command instead. `/update pre` includes
pre-releases.

With no service running, `polyrob update --apply` updates the code directly (it waits for
a REPL turn to finish) and then says `polyrob service restart` to pick it up.

## Settings that need a restart

Most settings are read when the service starts. When you change one —
`polyrob config set …`, `/config set …`, the console — the reply says so, and the service
keeps a list:

    waiting: 2 setting(s) apply at the next restart: TELEGRAM_ALLOWED_CHAT_IDS, X402_ENABLED

`/status`, `polyrob doctor` and `polyrob service status` show that line until you
restart. A few autonomy settings apply at once from the REPL; everything that touches
money, approvals or who may reach the agent always waits for a restart, on purpose.

## The agent and restarts

The agent cannot restart itself. At the self-maintain posture it may **ask**
(`self_env_restart_service`), and that request waits for your approval like every other
self-maintenance action; at any posture it can put a `/restart` or `/update` card in
front of you. Either way the restart is yours to confirm, and it runs the same drained
path as above.

## Running as root on a server

The documented server install runs the service as an unprivileged user (for example
`polyrob`) with linger enabled. You do **not** need that user's session:

```bash
# as root — finds the user that owns the polyrob service and acts as that user
polyrob service restart
polyrob service status
polyrob service logs -f

# several users run one? name it
polyrob service restart --user polyrob
```

`polyrob` runs the verb as that user, through the interpreter named in the unit, with the
user bus (`XDG_RUNTIME_DIR=/run/user/<uid>`) filled in. The same fix applies to
`sudo -u polyrob polyrob service …`, which used to fail with *Failed to connect to user
scope bus*.

A deployment that runs POLYROB as **system units** (`/etc/systemd/system/polyrob*.service`)
is restarted with `sudo systemctl restart <unit>` — the agent finishes its live turn
first (up to 60 seconds) — or with `/restart` from your chat.

## Older units and the first upgrade

A unit written before this release has no `RestartForceExitStatus=75` line.
`polyrob service status` says so; run `polyrob service install` once to rewrite it. Until
then `polyrob service restart` still works — it restarts through the service manager.

The update that brings this release in still runs the **old** `polyrob update`, which
refuses while the service runs. Do that one update by hand, as the service's user:

```bash
systemctl --user stop polyrob.service      # or: launchctl bootout gui/$(id -u)/dev.polyrob.agent
polyrob update --apply
polyrob service install                    # rewrites the unit with the restart contract, starts it
```

As root on a server, run those as the service user with its bus:
`sudo -iu polyrob env XDG_RUNTIME_DIR=/run/user/$(id -u polyrob) systemctl --user stop polyrob.service`
(and the same prefix for the other two). Every later update is `/update` or
`polyrob update --apply`.
