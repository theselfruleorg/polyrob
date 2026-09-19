# Upgrading

How to move an existing POLYROB install to a newer version, and the version-to-version
notes that need a decision from you.

---

## 1. Check, then apply

```bash
polyrob update --check            # exit 0 up to date, 10 newer, 1 unknown/error
polyrob update --dry-run          # print the plan, change nothing
polyrob update --apply            # snapshot → install → migrate → verify → auto-rollback
```

`--apply` is available for a **git checkout or an editable install**, where there is a
real runner to drive. Every other install method (pip, pipx, Docker, a system package
manager) gets honest per-method manual instructions instead of a button that would
pretend. `polyrob update` with no flags prints the install method it detected, the
current and latest versions, the schema state, and the right next command.

`--channel stable` (default) tracks releases, `pre` includes prereleases, `git` tracks
a branch. Exit 1 from `--check` means the release source could not be reached or
did not provide a usable version; it never means that the installed version is current.

### The safety net

`--apply` takes a snapshot before it touches anything, and rolls back on its own if
the verify step fails.

```bash
polyrob update --list-snapshots
polyrob update --rollback                    # the most recent snapshot
polyrob update --rollback --snapshot NAME    # a specific one
```

A snapshot covers your **databases** (every store in
`core/db_manifest.py::SIDECAR_DB_NAMES` plus the relational `bot.db` at its configured
`DB_PATH`), your **config files**, and the `identity/`, `skills/` and `wallet/`
directories under the data home. Snapshots exist only where something created one:
`--apply` and the boot-time migration are the two writers, and `--apply` keeps the
three most recent. So `--rollback` on an install that has only ever been upgraded by
its package manager has nothing to restore yet.

`--rollback` restores that data and only that data — it does **not** revert the code, so pair it with the
matching `pipx install "polyrob==<version>"` (or `git checkout`) if you are going
back a release. Reverting code automatically is the `--apply` failure path: if the
post-install verify fails, `--apply` puts both the code and the snapshot back on its
own.

Rollback refuses to run while the agent is in use; `--force` overrides that guard and
risks a corrupted database, so stop the service first instead.

> ⚠️ **A snapshot copies your `.env` files, including the wallet seed.** Treat the
> snapshot directory with the same care as the seed itself.

Schema migrations apply automatically at the next start, both for the server and for
the CLI container. In a git checkout you can run them explicitly:
`python -m migrations.migrate upgrade` (idempotent) and `… status`.

---

## 2. What a release can change

[`CHANGELOG.md`](../../CHANGELOG.md) is the dated, point-in-time history and is the
place to read before an upgrade. Three kinds of change need your attention:

- **A default flipping.** [`docs/CONFIGURATION.md`](../CONFIGURATION.md) always
  describes the *current* default. After upgrading, `polyrob doctor --flags --changed`
  shows only what you set away from a default — the fastest way to see whether a
  release moved the floor under you.
- **A flag being renamed or retired.** `polyrob config check` validates your env
  files against the flag catalog and names anything it does not recognise.
- **A tenant or path resolution change.** These are rare and are written up below,
  because rows do not move themselves.

After any upgrade:

```bash
polyrob doctor                    # providers, memory, autonomy, pauses, the active profile
polyrob doctor --flags --changed
polyrob config check
```

---

## 3. Version notes

### The unbound owner tenant is `local`

POLYROB used to have four resolvers answering "who owns this install?", and they only
agreed when an owner was bound. Unbound, the REPL and the CLI wrote under the tenant
`local`, the console and the x402 income stamp read `polyrob` (the *instance* id), and
`polyrob owner …` read the adopted instance id. There is one resolver now, and its
unbound answer is `local`.

**A bound install is unchanged.** If you set `POLYROB_OWNER_USER_ID` (or
`BOT_OWNER_USER_ID`, or `SURFACE_SUPER_ADMIN_USER_IDS`), every seat resolved the same
tenant before and after. The divergence was unbound-only. You can stop reading here.

#### Case 1 — no owner bound at all

An install that sets none of `POLYROB_OWNER_USER_ID`, `BOT_OWNER_USER_ID`,
`SURFACE_SUPER_ADMIN_USER_IDS` or `POLYROB_LOCAL_OWNER`, **and** used the console or
received x402 income. Rows written under the tenant `polyrob` become invisible to the
console, which now reads `local` alongside the REPL. Nothing is deleted; it is read
under a different tenant. What moves:

- console-created goals and cron jobs;
- x402 payment rows, and the `payment_unmatched` / `payment_self_proceeds`
  breadcrumbs;
- correspondents seeded by `polyrob owner invite`, and `owner_notice` rows;
- the per-room chat-policy overlay written by `/groups` and `polyrob owner groups`.
  ⚠️ **This one is behavioural, not just a hidden row**: an orphaned overlay leaves the
  room on its defaults, so a room you had set to `active` reverts to `mention` and
  stops answering unprompted;
- `polyrob apps …` and `polyrob skill …` rows;
- the wallet ceiling preferences (`budget.wallet_*`) a console wrote.

An install that used `polyrob owner …` after adopting an instance name (for example
`POLYROB_INSTANCE_ID=rob` with no owner declared) is in this case too: **an instance id
is no longer read as an owner tenant.**

**Remedy.** Choose one:

- Keep the old bucket — set `POLYROB_OWNER_USER_ID=polyrob` (or the adopted instance
  name). Every seat then reads that tenant.
- Or move the rows to `local` and leave the owner unbound.

#### Case 2 — `POLYROB_LOCAL_OWNER` set, with no explicit owner

⚠️ This install is affected **more** heavily than case 1, and it is easy to read the
list above and conclude you are safe because a key is set. `POLYROB_LOCAL_OWNER` used
to be read by the console alone; the one resolver now reads it everywhere, so the REPL
and the CLI move from `local` to that value. What moves: the session tree
(`data/auto/{user_id}/sessions/`), the goal board, the cron jobs, the memory rows,
`polyrob wallet book`, and everything in the case-1 list. The console does **not**
move — it already read that value, which is the point.

**Remedy.** Choose one:

- Keep the REPL and CLI where they were — unset `POLYROB_LOCAL_OWNER`. Every seat then
  agrees on `local`.
- Or accept the move and leave the key set. Every seat then agrees on that value, which
  is the state a bound install has always had.

#### Two things this also fixed

**Unbound owner-tier gates now compare `local` to `local`.** The owner *principal*
(the operand every owner gate takes) used to fall back to the instance id while the
owner *tenant* fell back to `local`, so an unbound install denied owner-tier capability
to its own owner. Both axes now answer the same value. On an unbound install, a goal or
cron run and the owner's Telegram DM regain owner-tier reach they only ever lost
through that split, and a deliverable built by a REPL- or console-created goal keeps its
attachments instead of having them silently stripped.

⚠️ Be precise about one of those. On an unbound install the tenant `local` now passes
owner gates **without** `POLYROB_LOCAL`, because it IS the owner principal there. A
bound install still requires the flag for that tenant, exactly as before. The flag's
behaviour did not change; what changed is that an unbound install no longer needs it
to recognise its own owner. Nothing else widens: the cross-tenant media guard still
strips for a genuinely different tenant, a network sender is still hashed to a `u_…`
id that no owner gate accepts, and `AUTONOMY_MODE=autonomous` still requires a bound
owner principal.

⚠️ A value the codebase treats as the anonymous bucket (`_anonymous_`, `system`,
`x402_user`, `authenticated_api_user`, `api_user`, or blank) is **not** a binding: it
falls through to `local` on both axes. It never was an isolatable tenant.

`POLYROB_INSTANCE_ID` is untouched — it still names the identity-document tier
(`identity/{instance_id}/`) and the avatar. See
[instances.md](instances.md).

---

## 4. Upgrading a profile, and upgrading a deployment

A **profile** is a whole isolated home, so it upgrades with the code around it. A
profile you installed from a git distribution has its own refresh:

```bash
polyrob profile update scout
```

That replaces the files the distribution owns (characters, skills, cron, `mcp.json`, a
shipped `soul.md`) and never touches your `.env`, credentials, wallet or the `data/`
tree. See [profiles.md](profiles.md).

For a **systemd deployment**, `polyrob update --apply` is not the path: a deployed
`/opt/polyrob` is an rsync TARGET with no `.git`, so `git pull` there fails, and
`--apply` exits non-zero with the manual steps for exactly that reason. There are two
honest paths, and `polyrob update` prints the one that fits your box:

**A tree deployed from a maintenance clone** upgrades through your deployer,
which should own the whole transaction: quiesce the unit family, snapshot the code
and the virtualenv, install (`pip install -c requirements.lock -e ".[<your extras>]"`
from the synced tree), migrate, restart, verify, and roll both back on failure.
Run it from the clone (`git pull --ff-only` first), never from the deployed tree.

**A wheel-shaped install** stops the units, upgrades the package, migrates and starts
again:

```bash
sudo systemctl stop polyrob.service polyrob-webview.service polyrob-email.service
pip install -U polyrob          # in the deployment's own venv
python -m migrations.migrate upgrade
sudo systemctl daemon-reload
sudo systemctl start polyrob.service polyrob-webview.service polyrob-email.service
```

Use `systemctl list-unit-files 'polyrob*'` when you are not sure which units the box
has. Then confirm it actually runs what you think it runs — check file content and the
process start time, not a recorded version string alone. Deployment details are in
[self-hosting.md](self-hosting.md).

---

## Where to look next

- [`CHANGELOG.md`](../../CHANGELOG.md) — what changed, dated.
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md) — every flag and its current default.
- [configuration.md](configuration.md) — how configuration resolves, and `polyrob config`.
- [migration/README.md](migration/README.md) — coming from another framework instead.
