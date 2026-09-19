# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.3] — 2026-09-19

### Added
- `polyrob x-account import-session <storage_state.json> | --auth-token … --ct0 …` and
  `capture-session --out <file>`: a desktop-captured X login can now reach a headless
  server. The encrypted session store is per-box (Fernet key + identity), so the hand-off
  is plain Playwright storage state (or the two login cookies), stored under the server's
  own key on import; a file with no `auth_token` is refused.
- **Autonomy scheduling.** A running board goal no longer starts just ahead of a due cron
  job (`GOAL_DISPATCH_CRON_HEADROOM_SEC`, also honoured while a job is mid-run), and a cron
  job classed `money` (`payload.priority`, set with `polyrob cron edit <id> --priority
  money`) can pre-empt a running board goal: the goal returns to `ready` with no failure
  counted and a `resume_note`, the rail runs on the same tick (`GOAL_YIELD_FOR_MONEY_RAIL`,
  default off). A human turn is never pre-empted. `polyrob cron edit --schedule '<spec>'`
  re-times a job with its next run recomputed.
- **Goal budgets.** `goal_create` gained `max_steps` (6–60) and `report_back`; the default
  budget for a goal that sets none is `GOAL_DEFAULT_MAX_STEPS` (30, was a literal 20). A goal
  created from a chat reports back in one line unless `report_back=true`.
- **Owner turns hold the shared workspace on headless surfaces.** A Telegram/email owner turn
  (like a REPL turn) marks the process busy, writes `<data>/locks/turn.active`, and takes the
  cross-process workspace lock — never refusing the human — so cron/goal ticks and the deploy
  waiter defer while you are mid-turn (`INTERACTIVE_GATE_MARKER`, default on; the lock dir is
  derived from `POLYROB_DATA_DIR` when unset). `/status` shows `turn: … active since HH:MM`.
- **Rail ledger on every status seat.** Every `cron_run` now ends with a terminal event —
  `done`, `failed`, `cut_by_cap`, `held` (owner pause), `deferred` (owner ask), or
  `cut_by_restart` (orphan reclaimed after a restart) — and the `loops` status section shows
  each enabled job's last outcome (`rail EXIT: done 04:18 · 7 steps · 6m`, or
  `started HH:MM, no end recorded`) plus a `rail_cut` health warning with the remedy.
- **External rails as health facts.** `/status` warns when the X browser rail is enabled
  without a stored login session (naming `polyrob x-account capture-session`), when the
  anysite user-search endpoint answers empty three calls running (`rail_probe` events), and
  when an SMTP login was rejected (`email_auth_rejected`), each with the remedy.
- **Filesystem verbs shaped for JSON-lines data.** `jsonl_append` (one compact line per
  object), `jsonl_remove(key, values)` (backup → rewrite → verify → atomic replace; one bad
  line refuses the whole rewrite), `jsonl_validate` (counts, bad lines, duplicates), and
  `copy_file` (byte copy, no overwrite unless asked — the backup-before-rewrite primitive).
- **`x_browser.x_reply`** — reply under an existing X post through the saved browser session
  (the lane the API tier refuses for non-mentioners); owner-approval-gated like `x_post`.
- **Skill revisions from background turns.** A background/forged turn's `skill_manage
  patch` of an active skill lands a pending revision under `.pending/<id>/` for the owner's
  `polyrob owner promote skill` — the active skill is never touched by a forged turn.
- **Stale session-directory GC** (`core/session_gc.py`): per-session dirs untouched for 14
  days are reported (dry-run, `session_gc` event) and removed only with
  `SESSION_DIR_GC_APPLY=true`; the first pass runs 10 minutes after start, then daily. The
  deployer purges the pip cache and vacuums the journal after a successful install.
- The deployer gains an idle-wait entry point: it waits (up to the cron ceiling) for
  no running cron job, no running goal and no live turn, then runs `deploy_prod.sh`
  (which now refuses a direct call unless `DEPLOY_FORCE=1`). Units are runtime-masked while
  the virtualenv is rewritten and unmasked on start and on rollback.

- `polyrob cron edit <id> --max-duration N` — change a scheduled job's hard cap
  (tenant-scoped, ≤1800 s like the agent tool; applies from the next run). The
  EXIT/SCOUT treasury rails had a 240 s cap and timed out on 22 of 24 runs; the
  cron ceiling itself rose 600 → 1800 s because the hourly buyback rail runs
  1-5 min per step and was cut at step 6 before its swap.
- The `/dev` owner→developer rail relays through a host spool (`<data>/dev_rail/`)
  drained by an owner-run unit, because the hardened service identity cannot reach
  the developer's tools directly.

### Fixed

- **Context overflow no longer kills a run.** The pre-LLM token check prunes once
  (`emergency_context_prune`) and re-checks before raising; a five-page tool step used to end
  the run with `Token overflow` and lose the round.
- **Posture is a ceiling, not a request.** `AGENT_COMPUTE_POSTURE>=1` adds `code_execution`,
  `shell` and `coding` to autonomous toolsets only when each tool's own flag is on; a
  deploy with the flags off no longer stamps a false `[tool gap]` line on every goal record.
  The flag predicates live in `core.config_policy.capability_toggles` (tools delegate).
- **`email` is dropped from the effective autonomous toolset while its SMTP login is
  rejected** (`core/credential_verdicts.py`, fed by the email tool's 535 path and cleared on
  the next success); the tool also remembers a rejected login for 15 minutes instead of
  re-sending bad credentials at every session start.
- **`append_file` given a JSON object wrote a pretty-printed multi-line record** into a
  JSON-lines store; it now writes one compact line per object. `coding_str_replace` coerces a
  JSON object passed as `old_string`/`new_string` to its compact line and says so in the
  result instead of failing validation.
- **`message` is not a missing tool.** The goal vocabulary infers the `message` action from
  "telegram"; the tool loader recorded it as `gated:unknown-tool` on every such goal.
  Action ids are recognised (`ACTION_IDS_NOT_TOOLS`); an absent action names its flag.
- **Docker socket unreachable → one honest refusal, not a retry storm.** `run_tests`/
  `run_code` refuse up front when the agent identity cannot open the Docker socket, naming
  the posture and "do not retry".
- `x_login_check` declares an explicit empty parameter model (ended a per-session WARNING).
- Twitter `get_timeline` accepts `max_results` 1–100 (the X floor is clamped inside the call).
- Browser stale-context reaper measures idle time, not allocation age.
- A message drained on the `done()` step earns the next step instead of ending the run.
- A host-level money-verb broadcast failure ends the run with the verbatim line.
- Root-run confined writes take the parent's service group (dirs 0770, files 0660).

- `filesystem_read_file` on an over-cap multi-line file returns a numbered tail
  window under a `TRUNCATED` header instead of refusing (append-only ledgers were
  refused 15×/6h on prod).
- H-MEM (`TaskContextManager`) base path was CWD-relative `data/auto` — read-only
  under `ProtectSystem=strict`; now `<data_home>/auto` (`DATA_PATH` still wins).
- One log file per service entrypoint (`bot.log` / `email.log` / `webview.log`,
  `POLYROB_LOG_FILE` override) — three non-root units sharing one rotating file
  raised `PermissionError` on every emit in two of them.
- `deploy_prod.sh` owns `$DATA_DIR/auto` for the agent identity (its root-run
  import-test created it 0700 root and every session then failed at init).

- **`polyrob update --apply` inside a foreign git repo.** A wheel whose
  site-packages sat inside somebody else's checkout (`/work/project/.venv/…`)
  was classified `git`, and `--apply` ran `git pull` + `pip install .` against
  THAT project. `site-packages`/`dist-packages` now never resolve a repo root.
  Also: `--apply` on pip/pipx/systemd/docker exits 1 with a structured `--json`
  payload (was exit 0 + nothing); the release `tag_name` is validated and
  checked out as `refs/tags/<ref>`; rollback is `git reset --keep`; one snapshot
  restore per failed step (`migrate_guarded.py` removed); `--json` never blocks
  on stdin; draft/pre-releases are filtered; snapshots pruned to 3 after an
  apply; `--channel git` measures the branch against its upstream instead of
  the release list; `ls /opt/polyrob` no longer counts as a running agent.
- **Migrations ran in filename order.** `v1_10_0_*` sorts before `v1_2_0_*`;
  `shipped_migrations` now sorts by parsed version. The boot-time pre-migration
  snapshot now includes the configured `DB_PATH` (on prod the one database
  being migrated was the one NOT backed up) and takes the updater's
  `update.lock`. `session_registry.db` joins the DB manifest.
- **Deployer.** One `pip install -c requirements.lock -e "/opt/polyrob[…]"`
  replaces a non-editable install that left a SECOND code copy in
  site-packages (4 of 7 units ran that copy); every active unit sourced from
  `deployment/` is reconciled (the webview unit carrying the de-root hardening
  never was); `polyrob-browser-server.service` is quiesced with the family;
  free space is checked on `$TMP`'s mount; the character preflight honours
  `POLYROB_DATA_DIR`; the wallet chown is guarded; the import test covers the
  telegram surface.
- **`polyrob serve` trusted `X-Forwarded-For` from anyone** — default is now
  `127.0.0.1`; new `UVICORN_FORWARDED_ALLOW_IPS`.
- **`polyrob profile create --service`** wrote a weaker `polyrob-<name>.service`
  (collided with `polyrob-email`); it now renders the committed
  `polyrob@.service` template (byte-pinned) and enables `polyrob@<name>`.
- `polyrob approvals *` / `polyrob config set` act on the DEPLOYED data home.
- `setup_publish_vhost.sh` chowns the publish root to the agent (first publish
  EACCES'd) and no longer guesses a port for `/api/`.

### Changed

- `requirements.lock` is `uv pip compile --all-extras --universal` (every extra
  pinned, none installed unless named); `requirements.txt` is a thin pointer
  (`-c requirements.lock` + the prod extras). Root units that stay root gain
  `Group=polyrob-data` + `UMask=0002`. `.dockerignore` added; the image sets
  `UVICORN_HOST=0.0.0.0` + `POLYROB_IN_DOCKER=1`. `release.yml` runs only on
  the public repository.

### Removed

- `webview/webview.service`, `scripts/publish_prune.sh`,
  `deployment/nginx_continuous_chat_fix.conf`, `.github/workflows/deploy-portal.yml`;
  the api+webgate units, `nginx.conf`, the Xvfb trio and the SSL scripts moved to
  `deployment/legacy/`; `deployment/CLEAN_DEPLOY.md` retired.

## [1.0.2] — 2026-09-18

### Added

- Fly.io single-tenant container posture (`deployment/fly/`): a runtime image
  (python + `polyrob[all]` + Chromium + tmux + git + the native Claude Code CLI),
  a one-machine `fly.toml` with NO services and no IP (nothing listens;
  Telegram is outbound), an entrypoint that clones the private repo onto the
  volume (credential store, never a token in the URL), layers the committed
  non-secret flags UNDER the Fly secrets, installs an instance kit on first
  boot and runs three processes as one non-root user: the agent runner
  (`touch /data/restart.agent` = clean restart after a `git pull` = deploy),
  the Claude dev `/loop` in tmux (the `/dev` Telegram rail pastes into it),
  and a watchdog. Dangerob gets `dangerob.fly.env` + a container-specific
  `dev-loop-prompt.md`. `deployment/fly/README.md` lists what to buy and which
  secrets to set.
- X OAuth 2.0 user token: encrypted store + **auto-refresh** (`tools/x_oauth2.py`).
  The X Chat DM read (where every inbound DM now lands) needs a user-context
  OAuth2 token that X expires two hours after mint; the tree read ONE static
  env value and never refreshed it, so a hand-minted token proved the rail once
  and then inbound went dark. Every consumer (`surfaces/x/client.py`,
  `tools/twitter_tool.py`, the `polyrob x`/gateway presence checks, the CLI
  twitter gate) now resolves through `resolve_access_token`: store → refresh
  within 5 min of expiry (refresh token ROTATED and persisted before the old one
  is dropped; a failed refresh keeps the old pair) → env seed
  (`TWITTER_OAUTH2_ACCESS_TOKEN` + new `TWITTER_OAUTH2_REFRESH_TOKEN`, stored
  once) → static env override. The DM client retries a 401 exactly once on a
  refreshed token; the twitter tool rebuilds its DM + Chat clients before every
  DM read/send. `polyrob x-account oauth-login` (PKCE with a local callback),
  `oauth-import` (hidden prompts), `oauth-status`, `oauth-refresh`. New flags
  `TWITTER_OAUTH2_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN`.

### Changed

- **The per-transaction wallet ceiling is the owner's, from chat** (owner ruling
  2026-09-18, after the second time an approved raise did nothing).
  `budget.wallet_per_tx_usd` is now an owner-override preference: an approved
  value replaces the `AGENT_WALLET_MAX_PER_TX_USD` default in either direction,
  clamped to the daily cap (`core/wallet/config.py::effective_max_per_tx_usd`).
  The daily cap (`budget.wallet_daily_usd` over `WALLET_DAILY_CAP_USD`) stays
  min-merged and env-only — it is the operator's hard envelope, so a single
  transaction can never exceed what a day may lose and no raise made from chat
  moves the maximum daily loss. Prod 2026-09-17: the owner approved
  `wallet_per_tx_usd = 220`, the pref sat on disk, the guard kept reading the
  env's $120, and the agent asked the owner to edit `polyrob.env` and restart.

### Fixed

- **The wallet caps apply LIVE.** `PolicyGate` copied both caps at construction
  (process start), so `budget.wallet_per_tx_usd` / `budget.wallet_daily_usd` —
  documented and shown as `applies: live` — took effect only at the next
  restart, in EITHER direction (an owner tightening from chat was just as
  inert). `WalletConfig.cap_resolver` (`live_caps_resolver`) is consulted on
  every `check()` and by the cap properties; a hand-built config keeps the
  frozen values; a raising resolver keeps what the gate had.
- **The approval pre-hook read the raw env ceiling, not the owner's.**
  `spend_lane.autonomous_ceiling_usd()` read `DEFI_AUTONOMOUS_MAX_USD` while
  `tx_guard` step 9 read the pref-resolved `budget.defi_autonomous_usd`, so the
  two halves of one lane disagreed: the owner approved a $300 autonomous
  ceiling, tx_guard honoured it, and the hook still demanded a tap for every
  live swap over the env's $5 — three taps in one afternoon for trades the
  owner had said may run unattended, and an unattended cron buyback that could
  never run. The hook now delegates to `tx_guard.autonomous_max_usd` (fail-open
  to the env value, never wider).
- **Approving an ask a CRON run raised re-arms the job instead of waking the
  dead run.** The ask had no goal to re-arm, so resume-on-grant self-woke the
  finished cron session — a forged turn the money guard refuses — and the agent
  told the owner "trigger it from your seat" for a trade the owner had just
  approved (prod 2026-09-17 16:14). Autonomous sessions now remember their cron
  job (`autonomy_marker.cron_job_for_session`, threaded through
  `run_task_to_outcome(cron_job_id=)`), the ask carries `cron_job_id`, and an
  approval pulls that job's `next_run_at` to now (`core/cron_rearm.py`, the one
  UPDATE both `CronJobStore.run_now` and the owner queue use) so the next tick —
  a genuine cron turn — redeems the grant. No wake. A running job is left alone
  and the owner is told its next run redeems the grant.
- **A guarded proposal that could never take effect is refused, not queued.**
  For a min-merged key with the env set, a value above the env value resolved
  to the env value after the tap — the owner saw "tap to approve", tapped, and
  nothing moved. `propose_pref_change` now refuses up front and names the exact
  env line (`WALLET_DAILY_CAP_USD=…` in the service env file) that would.

- `FileTokenStore` writes follow the shared-data identity convention
  (`<writer>:polyrob-data 0660` under a group-writable data dir; an existing
  file keeps its mode + group). It wrote `0600` in the writer's primary group,
  so the OAuth2 pair the root CLI imported was unreadable by `polyrob-agent`
  — the unit that needed it — and an agent-written refresh would have been
  unreadable by the owner's CLI the same way.
- `FileTokenStore` no longer treats an UNREADABLE file as a CORRUPT one. On
  prod the `polyrob-email` unit (a non-root service identity) got `EACCES` on
  the root-owned `0600` X token store that `polyrob.service` had just written,
  and the corruption branch renamed the file aside — deleting the agent's
  freshly imported OAuth2 pair from under the process that owned it. An
  `OSError` on read now logs and yields an empty in-memory store for THAT
  process only; the file is left where it is.

- **The browser rail under wallet custody (proposal 049).** A custody process
  refused local Chromium — correctly — but never reached the isolated browser
  it was told to use: `BrowserManager` passed the `BrowserConfig` into
  `Browser`'s BotConfig slot, so `BROWSER_CDP_URL`/`BROWSER_WSS_URL` were
  dropped (prod 2026-09-17: the service ran, the env was set, 4,517 refusals in
  24 h, and the agent told the owner "no remote browser configured"). Over CDP,
  every session then attached to the service's default PERSISTENT context — no
  `storage_state` (the X login silently dropped), no service-worker block, one
  cookie jar shared by every session and tenant. Both fixed: the endpoint
  reaches the browser, and a remote browser always gets a fresh context.
- Owner-ceremony launches (`x-account capture-session`/`signup`, the pfp still)
  now consult the one launch policy: refused under custody, scrubbed env,
  sandbox on. `tests/test_browser_launch_ratchet.py` pins the launch sites.

### Added

- `core/security/browser_rail.py`: the ONE answer to "can this process drive a
  browser, and through what" — `none (custody) → install`, `configured,
  unreachable (<reason>) → check the service`, `remote cdp ok (<version>)`.
  Read by the launch refusal, the step loop (which now skips the per-step page
  observation and warns once instead of three ERRORs per step), the
  `<tool-catalog>` (`gated:custody-no-browser` for `browser`/`x_browser`/
  `dapp_browser`), the status snapshot's `identity` section (`browser:` line;
  WARN only when a configured endpoint fails) and `polyrob doctor`.
- `polyrob browser install|update|status|render`: the isolated browser
  service as a boundary — dedicated UID, Chromium **sandbox on** via an AppArmor
  `userns` profile (Ubuntu 24.04 restricts unprivileged user namespaces; the
  earlier unit's `--no-sandbox` was a wrong diagnosis of that), a UID-keyed nft
  egress chain (no loopback / RFC1918 / link-local / metadata from the browser),
  Chromium for the venv's Playwright pin. `deployment/polyrob-browser.service`,
  `polyrob-browser-egress.service`, `hardening/polyrob-browser-egress.sh` and
  `hardening/apparmor/polyrob-browser` are the CLI's rendered output, pinned by
  a test. `scripts/deploy_prod.sh` prints the browser revision beside the pin
  and updates on drift. `--mode server --listen <private-ip>` on a SECOND host
  writes a Playwright-server unit instead (token in a root-only env file,
  private bind enforced) and prints the agent's `BROWSER_WSS_URL` — the shape
  that removes the shared kernel. Guide: `docs/guide/self-hosting.md`.

### Fixed

- The REPL painted 5–10 blank rows above the prompt for every provider error
  and never showed the error text: `core/logging.py` gave the file handler
  and the console handler ONE `ComfyFormatter`, whose 0.5 s duplicate filter
  saw the console's copy of each record as a repeat of the file's and
  returned `""` — the `StreamHandler` still wrote the newline, and every such
  write erased and redrew the prompt. Duplicate suppression is now a
  per-handler `logging.Filter` (a formatter cannot drop a record), and the
  REPL console renders one compact `✗ component: message` line per error;
  a re-wrapped exception (one 401 logged five layers of the same text) is
  collapsed to its first line on the console only — `bot.log` keeps every
  layer.
- Gemini: a text part was dropped and logged as `Gemini function_call
  missing name` at ERROR. A proto `Part` answers `hasattr(part,
  "function_call")` True for every oneof member, so a plain text part hit the
  nameless-call branch and `continue`d — every text-only Gemini reply became
  "Model output has empty action list", the model was pushed into a filler
  step, and the REPL showed a second bubble ("Я жду твоего ответа"). Presence
  is now asked via `"function_call" in part`.
- A session the terminal created (REPL, one-shot `polyrob run`) is a live
  surface: it renders every reply from the feed but bound no router, so
  `maybe_deliver_autonomous_send` pushed each chat reply through the owner
  delivery rail — dedup, the hourly rate limit and the daily cap included.
  Past the cap `send_message` told the model its answer was "NOT delivered",
  the model re-sent an apology (another duplicate bubble) and the turn closed
  `failed`. `core.surfaces.binding.bind_terminal_surface` marks such a
  session; `--resume` keeps the durable rail (2026-08-28 incident).
- The bounded planning turn (`ALLOWED_REASONING_TURNS`) no longer logs two
  ERROR lines ("violates the agent contract") before the caller grants it.

### Changed

- `done(text)` no longer reaches the user on any chat surface. `done` writes
  the run log (history + session feed) and nothing else; only `send_message`
  / `message` speak, in a DM as in a room — exactly what the prompt has told
  the model since C1. The old router mirror published `done` whenever
  `send_message` had not claimed the turn, and the `message` tool (a file
  with a caption) never claimed it, so on prod (2026-09-17) the owner received
  the answer AND a 2,400-char third-person session recap after every such
  turn. The unbound HTTP paths (`chat_once`, `/v1/chat/completions`) still
  fall back to `done`'s text for the response body when the turn spoke
  nothing — a request needs a body.

### Removed

- `CHAT_SINGLE_FINAL`. Its only consumer was the `done` mirror's latch gate;
  with the mirror gone there is no second voice to gate, and a flag would be
  a knob on a rule the prompt states without one. `core/surfaces/turn_reply.py`
  keeps only the reply-text record the unbound paths read.

## [1.0.1] — 2026-09-17

### Added

- `self-deploy` skill: an agent-facing bootstrap for a FRESH instance —
  assess (model, wallet + funding per chain, email, X, autonomy grants, tool
  catalog), provision what a lever exists for (own inbox, X account, standing
  work), bundle the human-only asks into ONE message (API keys, funding
  addresses read from a tool, env flags, a CAPTCHA), verify by a READ, and
  report one readiness table. Until now `polyrob init` was the human's wizard
  and the setup interview covered only the owner contract.
- `twitter_poll_results(tweet_id)`: reads the options, votes, shares,
  `voting_status` and end time of a poll the agent posted. The tool could post
  a poll (`poll_options`) but no read asked for `attachments.poll_ids`, so the
  agent could ask the community and never learn the answer. The
  `x-engagement` skill now teaches the post → record id → read → aggregate loop
  and pins poll answers as DATA.
- X message reads distinguish legacy Direct Messages from encrypted X Chat,
  support OAuth 2.0 PKCE user tokens, and can verify/decrypt Chat event history
  with the official Chat XDK and account keys instead of misreporting ciphertext
  or a legacy-only page as an empty inbox.
- The `identity` status section now reports the agent's reachable identities:
  `email:` (the address it sends AS, or `none → remedy`) and `x:` (API rail
  configured / PARTIAL with the missing key names / none, plus the handle and
  whether writes are armed). Env PRESENCE only, never a value.
- A `liquidity` status section on every seat (status snapshot, `polyrob wallet
  overview`, Telegram `/wallet overview`, console `GET /api/webgate/liquidity`)
  lists the treasury's Uniswap v3 positions with their fees; on-chain
  enumeration is opt-in and owner-only. `/lp` is listed in the money verb
  group on every chat surface.
- `launchpad_status` names the graduated pool (PoolKey + pool id) once a Pons
  V2 curve has graduated, and states the fee reality: the LP fee is 0, the
  hook collects, creator income is claimed via the escrow, and the launch
  locker position cannot be withdrawn.
- `X_SIGNUP_HANDLE` / `X_SIGNUP_DISCLOSURE`: the @handle `x_signup_start`
  requests and the automation-disclosure bio it writes.

### Changed

- Post-1.0 alignment sweep: one home per shared rule instead of hand-carried
  copies. `core.event_log` owns the telemetry db resolution and a fail-open
  `emit()` (the recap reader used to ignore `TELEMETRY_EVENT_LOG_PATH` and
  create the db on read); every owner/admin CLI verb resolves its data home
  through `cli/_admin_home.py` (the deployed-home rule now also covers
  `apps`, `surface`, `cron`, `goals`, `journey`, and `owner pending`'s goal
  board, which came from a third resolver); the sub-agent and owner-pause
  refusals every money verb states live in `core.wallet.authority`; the
  publish/app-service owner-turn clauses in `core.security.owner_turn`; the
  ship rail's orchestrator/approval/workspace helpers in `tools/ship_common.py`;
  the goal board's status/kind vocabulary in `core.goal_vocab`; the `/help`
  group order is read from `core.verbs`; the copy-layer engine is shared by
  `core.copy` and `webview.copy`; the Telegram update dedup is the core
  `IdempotencyStore` over its existing table. Twelve open-coded boolean
  truth sets now parse through `core.env`, pinned shrink-only by
  `tests/test_bool_env_parse_ratchet.py`.
- Social-agent skills and toolsets now route public discovery, native X account
  reads, encrypted Chat, browser inbox fallback, and approval-gated writes through
  the capabilities that actually implement each operation.
- REPL slash commands parse quoted arguments (`/steer "two words"` is one
  argument); an unbalanced quote is refused with the remedy. Free-text verbs
  (`/learn`, `/persona`) keep the raw line. `/export` names the formats the
  REPL supports and points at `polyrob session export` for the rest; `/logs`
  prints the log directory instead of a CLI verb that does not exist.
- The approval `awaiting` event names the provider that will decide.
- `lp_collect` / `lp_remove` set their receipt minimum from a simulated
  collect, not the position's fee-growth estimate (core rounding leaves the
  estimate several raw units high, which made a correct collect refuse).
  Every position receipt also asserts the fungible legs. LP legs are no longer
  written into the token-keyed position book: adding them double-counted
  tokens already held, and removing them on withdrawal erased unrelated
  holdings. Position receipts stay in the NFT and transaction telemetry until
  an LP-specific basis exists.

### Fixed

- Scheduled agent runs now propagate wall-clock cancellation and are recorded as
  incomplete unless the agent actually calls `done()`. The default cron budget is
  ten minutes, preventing slow provider calls from silently turning unfinished
  treasury rails into successful ticks.
- Provider health distinguishes a sentinel on the serving provider from one on an
  unused fallback. A credit-limited fallback is shown as a warning and explicitly
  does not claim to block the live provider.
- Live incremental streams claim the turn's reply latch after becoming visible,
  preventing `done()` bookkeeping from producing a second user-facing response.
- Browser management accepts operator-configured CDP and WebSocket endpoints, so
  hardened custody deployments can keep Chromium outside the signing service.
- The owner's spend ceilings are read from the same home the preference
  writers use. `tx_guard`, the wallet config fallback and the Telegram
  `/wallet autonomous` verb resolved the preference store through the
  process home (an empty tree under a service account), so an approved
  `budget.defi_autonomous_usd` was recorded and never read and the guard kept
  refusing at the env default. Those seats may no longer call the process
  home (ratcheted).
- X self-registration: `XPageDriver.set_handle_and_profile` was a `pass` stub
  while the docs claimed the disclosure was written into the bio — no handle
  and no disclosure were ever applied. It now edits `/settings/screen_name` and
  `/settings/profile` best-effort and RETURNS what it applied; the signup
  result carries `requested_handle`/`handle_applied`/`bio_applied` and the tool
  says plainly when the disclosure is NOT on the profile yet. The flow refuses
  up front with the remedy when the agent has no email or no inbox client
  (it used to fill an EMPTY email and pause several steps later on "no code
  arrived"), provisions the AgentMail inbox idempotently before opening a
  browser, and the default disclosure names the instance instead of the
  owner's internal tenant id (`operated by local`).
- `x_browser` stays explicit-grant-only: it is `high_impact` +
  `delegate_blocked`, so it is not on the CLI optional-registrar table and is
  reached by explicit `tool_ids` only.
- A per-profile daemon (`polyrob profile create --service`) no longer loads
  the primary instance's environment first: any key the profile did not
  override leaked through (its Telegram token → a 409 fight, its `TWITTER_*`
  keys → the profile posted AS the primary, its owner ids and money flags). A
  profile daemon reads its own environment files only.
- `defi_trade.register_agent` / `set_agent_uri` referenced `os.environ` with
  no module-level import (latent `NameError`).
- The coding and git tools resolve their confined root per TENANT, not the
  anonymous bucket, on a multi-tenant server.
- The `twitter` extra now carries `chatxdk`, which the encrypted X Chat reads
  need; a base install without the extra still imports.

### Deployment

- The signing service owns the wallet state directory while wallet artifacts
  remain group-readable, allowing the durable submission journal to be created on
  the first broadcast without weakening the service-identity boundary.

## [1.0.0] — 2026-09-16

POLYROB 1.0 is the first stable public release. It brings the terminal, web
console, chat surfaces, autonomy runtime, tool system and optional wallet rails
under one documented control model, with explicit owner authority and
fail-closed boundaries around credentials, sessions and money.

### Agent, CLI and operator control

- The terminal client now supports interactive chat and one-shot runs with
  task files or stdin, file and image attachments, JSON/JSONL results,
  profile-scoped history, persistent background delegation receipts and full
  session identifiers.
- Safe controls can pause, steer or stop a busy session at step boundaries.
  Cross-process controls are acknowledged, keyless stops are supported, and
  update/rollback refuses while a live process owns a database unless the
  operator explicitly forces it.
- Status, doctor, owner, wallet, session and REPL views share the same typed
  runtime snapshot. Provider fallback updates the displayed provider/model only
  after the main agent actually switches.
- A turn commits one user-facing reply. Completion bookkeeping no longer
  produces a second chat message, and workspace paths resolve to attachments,
  console links or an honest server-only state.

### Web console

- The console now has one responsive five-destination shell for Chat, Work,
  Money, Inbox and Agent. The legacy page set and `WEBVIEW_UI` switch are
  removed.
- Work shows running goals, schedules, cron jobs and live sessions, and can
  create goals and schedules through the same guarded writers used elsewhere.
  Money presents typed positions, movements, invoices and limits. Agent exposes
  identity, memory, skills, capabilities and permitted configuration controls.
- The console includes durable light/dark/automatic themes, a shared command
  palette, live Socket.IO refresh, paginated chat summaries, typed artifacts
  and narrated tool activity.
- Inline scripts were removed from the console CSP. Owner routes, session
  transcripts, files and event streams are tenant-scoped; logout revokes the
  active token and authentication storage failures deny access.
- Console mutations rely on the HttpOnly session cookie. The standalone proxy
  forwards authenticated identity to the task API and renders structured
  refusals. Money includes an owner-scoped, read-only wallet view with safety
  labels, trusted cached balances and unresolved-submission warnings.

### Autonomy, groups and owner authority

- Telegram groups have a public-session profile, per-chat roles and policy,
  bounded toolsets, reply threading, a room ledger and owner-only administration
  from direct messages. Denied room turns do not leak owner facts or private
  notices into the room.
- Optional paid room actions use expiring offers, asset-aware x402 invoices,
  settlement verification, effect receipts and credits when a paid effect
  cannot be applied.
- Owner stop instructions persist across restarts. Paused streams stay paused,
  only an owner can resume global autonomy, and owner rules distinguish
  restrictive instructions from permission-granting changes.
- Delivery suppression is observable and recoverable: missed notices, caps,
  quiet-hour holds and unavailable producers are represented in status rather
  than silently counted as delivery.

### Skills, memory, models and identity

- Curated character profiles ship in wheel and source distributions. Persona
  selection, authoring and inspection are available from the CLI, REPL and
  console, and the neutral stock avatar now ships without instance-specific
  identity.
- Skill triggers, action names and tool IDs are checked against the runtime
  index. Trading, token assessment and verified-tooling guidance cover the
  capabilities exposed by the current tool catalog.
- Owners can add, test and remove HTTPS MCP servers from the CLI, REPL or
  Telegram. Persisted servers reload per tenant; stdio entries remain local
  configuration and are refused in wallet-custody processes.
- Model profiles and auxiliary routing were expanded, while prompt catalogs and
  recent context are kept within model size ceilings.

### Optional wallet, payments and on-chain tools

- The optional wallet stack supports guarded EVM and Solana swaps, bridges,
  token deployment, contract calls, launchpads, dapp sessions, NFT inspection
  and transfer, revenue collection, ERC-8004 identity, asset-aware x402
  settlement and initial Uniswap v3 liquidity reads and writes.
- Money tools remain disabled until their feature flags, tool grants, owner
  approval and spend policy all allow the action. Unpriced approvals and
  undeclared token or NFT movement are refused; submitted and confirmed effects
  are recorded separately.
- Durable spend reservations are serialized across processes. Corrupt or
  unreadable accounting blocks further spending, chain and venue attribution is
  retained, and receipt reconciliation checks the landed effect before marking
  an action complete.
- Token assessment adds resolver fallbacks, holder concentration, price history,
  new-pool discovery and composite screening with explicit partial/unknown
  states. These rails remain experimental and off by default.

### Security and release integrity

- Evaluation runs default to reviewed fixture scenarios with sealed run,
  configuration and source identity. Missing, unknown, drifted or mismatched
  evidence fails scoring, and unbudgeted live evaluation seeding is disabled.

- Child processes receive a credential-scrubbed environment. Managed RPC URLs,
  bearer tokens and provider keys are masked in configuration, logs, stored
  results and terminal output.
- Browser and fetch paths enforce URL and redirect checks, block protected
  network ranges, reject compressed responses, pin validated addresses and use
  one deadline across DNS and redirect hops. Untrusted content is framed at
  model and browser-render boundaries.
- Host execution is refused when wallet custody or a payment/deposit master seed
  requires a sandbox, including when the agent wallet itself is disabled.
- Session tokens require finite expiry and revocation identifiers. API request
  bodies, command output and concurrent reads are bounded; cancelled host and
  container processes are reaped.
- External payment, exchange and treasury submissions journal their local
  intent, signature or transaction hash before broadcast. Ambiguous outcomes
  keep the spend interlock held until reconciliation, preventing a blind retry.
- EVM and treasury sends reserve before signing. Submission-journal identity and
  high-water checks refuse missing or truncated history; venue attempts cannot
  be released by smaller charges or charges for another venue. A read-only
  recovery diagnostic checks EVM/Solana chain evidence while retaining every
  reservation. Shared urllib RPC replies have raw byte and encoding limits;
  malformed or unrelated EVM receipts remain pending.
- Treasury sweeps verify the configured chain and derived signing address,
  reserve the signed gas cost, use pending nonces and require a matching
  successful receipt before bookkeeping can complete.
- Chromium launches with sandboxing enabled. Sandbox opt-out is restricted to
  local non-custody development, failed startup reaps the driver, and remote
  browser credentials are redacted from connection errors.
- Package manifests now carry the migrations, console assets, avatar engine,
  character library, contract sources, browser DOM helper and bundled fonts
  used at runtime. Release checks install the wheel into a bare environment and
  exercise version, help, doctor and migration commands before publication.
- Knowledge ingestion reads bounded, descriptor-relative snapshots; rejects
  symlinks, hard-link aliases and special files; and hashes the same bytes it
  parses. DOCX admission bounds archive expansion, part and table counts, XML
  structure and extracted text, while failed replacements preserve existing
  knowledge.
- Upload capacity remains reserved until temporary request storage is consumed
  or closed, binary sniffing is bounded, and mismatched content lengths are
  rejected.
- Wallet audit locks are reusable only by the asyncio task that owns the spend
  reservation; unrelated tasks and worker threads cannot borrow another task's
  reservation.
- Package builds now require setuptools 83.0 or newer.
- Skill writes and promotions validate tenant identifiers, refuse linked paths,
  use descriptor-relative atomic writes and archives, and fail closed when the
  threat scanner is unavailable. Project-context snapshots are bounded.
- Acceptance declarations are validated before execution or goal creation. File
  probes are confined, artifact IDs bind to their goal and session, hash/content
  checks use one snapshot, and HTTP probes pin public addresses and revalidate
  redirects.
- Provider recovery adopts the shared model state, restores the requested native
  tool mode and refreshes capability catalogs before each step. Source precedence
  separates current owner intent from factual evidence.
- Short untrusted tool strings are framed, final action evidence survives long
  runs, test verification requires real comparable results, and memory corrections
  do not delete nonidentical facts solely from embedding similarity.
- Knowledge sources publish chunks and hash/count metadata atomically, preserving
  the previous source on failure and excluding stale vector-cache content.
- Directory discovery is bounded by output, entry count, depth and elapsed time.
  PDF and DOCX parsing runs in admitted subprocesses with cancellation cleanup;
  Linux adds an address-space limit.

### Breaking changes and migration

- The version advances directly from 0.13.0 to 1.0.0; no 0.14.0 release was
  published.
- The legacy console routes and `WEBVIEW_UI` selector are removed. Use the five
  destination console and `/api/webgate/*` endpoints.
- Public group sessions no longer inherit owner memory, project context or
  owner-only tools. Deployments that relied on broad group access must configure
  explicit per-chat policy and tool grants.
- Review the [upgrading guide](docs/guide/upgrading.md),
  [configuration reference](docs/CONFIGURATION.md) and
  [security model](docs/guide/security-model.md) before enabling autonomy,
  wallet custody, code execution or public chat surfaces.

## [0.13.0] — 2026-09-08

### Two-week release audit — money, autonomy and app-service hardening (2026-09-08)

A full audit of the two weeks since 0.12.0 produced 6 critical and 13 high findings;
every one is fixed with a regression test.

- **Solana swap guard sees the whole transaction** (`core/wallet/solana_tx_inspect.py`): a
  Token-2022 transfer no longer bypasses the balance-delta check, the guard reads the
  full instruction set instead of the first transfer, and it REFUSES what it cannot
  observe rather than passing it.
- **A Solana swap reports whether it actually landed** — a submitted-but-unconfirmed
  signature is no longer recorded as a completed trade.
- **A real x402 payment is no longer swallowed** by the settlement watcher while one of
  our own swaps moves the same amount through the treasury in the same window.
- **`polyrob autonomy pause` writes to the resolved data home**, not the process CWD —
  the CLI could previously report a verified pause that the running agent never saw.
- **`/pause` stops the `message` tool** (the dominant autonomous send path) and the
  `wallet` daily spend cap + replay guard now hold ACROSS processes, not per-process.
- **The cold-start requeue no longer rips a goal from a live claim** — the boot sweep
  takes the same CAS guard the dispatcher does.
- **A goal run that produced every deliverable is no longer failed** for a missing
  `done()`, and an unanswered owner approval no longer counts as a cron job's failure.
- **A goal says WHICH declared tool never loaded, and why**, instead of failing opaquely.
- **The stream seeder takes a lock** so a manual run cannot double-seed a money leg;
  planner stall/backoff bookkeeping is tenant-scoped.
- **The agent recognises a sub-addressed copy of its own address** (`me+tag@…`) as itself,
  closing a self-correspondent binding loop.
- **Sandbox container uid can edit host-written files**, not merely enter their directories.
- **The Alchemy key cannot reach the log** from the DeFi index provider.
- **App service (032) hardening:** approval binds to the approved CONFIG (not just the
  slug), app egress admits only public addresses and is re-asserted every supervisor tick,
  and the tested-tree digest describes exactly the tree that ships.

### Telemetry — every external write records its effect (2026-09-08)

- **`wallet_spend` rows carry the tenant.** They were written tenantless, so the unified
  ledger read a confident `$0.00` over real spend.
- **Cron delivery goes through the gated `twitter` / `email` ACTIONS**, not the raw tool
  helpers — so a scheduled post is rate-limited, approval-gated, cooldown-checked and
  recorded like every other send.
- **`/pause social` actually stops autonomous posting.** Seven of the pause scopes had
  zero consumers; the social scope is now enforced at the send path.
- **Shrink-only ratchet** (`tests/test_external_write_telemetry_ratchet.py`) pinning every
  external writer that still records no effect telemetry, so the list can only get smaller.

### Refactors (2026-09-08)

Behaviour-preserving extractions that bring three oversized modules back under the size
ratchet: the controller's document-authoring actions, `TaskAgent`'s public session-control
verbs, and the LLM manager's read-only client/model inventory each move to their own mixin.

### 032 — durable app service (2026-09-07)

An agent-built app now runs as
its own hardened container on the box, survives session end / agent restart / reboot,
and is reachable at `https://<slug>.<APP_SERVICE_BASE_DOMAIN>`; the owner approves the
address once and `/halt` stops it. Off by default (`APP_SERVICE_ENABLED`); ONE bundle
turns it on: `AGENT_BUILDER_MODE=ship` (+ `APP_SERVICE_BASE_DOMAIN`).

- **Registry** (`core/app_service/registry.py`, `app_services.db`): tenant-keyed rows,
  address-sticky approval, CAS claims, caps counters; a cross-tenant slug rejects.
- **Tool** `app_service` (`deploy`/`stop`/`list_apps`/`logs`): the `publish` gate shape,
  the 031 pause predicate, workspace confinement, ship==tested, caps, secret-shaped env
  refused. **The pending row is the owner ask** (the owner-queue provider denies an
  autonomous goal-run turn with no ask — the turn the `ship-software` stream uses).
- **Supervisor** (`polyrob apps supervise`, `deployment/polyrob-apps.service`): snapshot
  of the tested tree, per-app bridge + nft egress deny, `docker run -d` with the ONE
  hardening list (`core/container_hardening.py`, lifted from the sandbox backend),
  health, a two-substitution nginx stanza, `artifacts.url` stamped on go-live (021),
  breaker, logs; pause edge tears live containers down and resume redeploys.
- **Owner seats**: `polyrob apps …`, Telegram `/apps …`, REPL `/apps`, the console Apps
  page, the status snapshot `apps` section (pending approvals lead as CRIT).
- **`AGENT_BUILDER_MODE=off|build|ship`**: the fifth named default bundle — `build` =
  `PUBLISH_ENABLED` + `GITHUB_TOOL_ENABLED` and `publish` in the goal toolset; `ship` =
  + `APP_SERVICE_ENABLED`/`APP_SERVICE_ALLOW_PUBLIC` and `app_service` in the goal
  toolset; clamps to `build` without domain + cert; never money/host/secrets.
- **Serving side** (owner-run once): `scripts/setup_apps_vhost.sh` — wildcard cert via
  manual DNS-01, the nginx include dir, the unit. `deploy_prod.sh` restarts it with the
  family. Guides: `docs/guide/deployment-postures.md`, `docs/guide/owner-controls.md`.

### Publishing & app-deployment evaluation — Wave 1 (2026-09-06)

A production audit found 336 agent artifacts, 0 with a URL, and the built
`publish`/`hf_deploy`/`github` rails all switched off. Wave 1 stops the ship loop from satisfying itself. Wave 0
(enable the built static rail on the box) is an owner step; Wave 2 is proposal 032.

- **`ship-software` stream** (`data/streams/streams.yaml`): the grant now carries
  `publish`, `shell`, `process` (the goal body already told the agent to use the process
  tool the grant omitted); the success criterion demands a URL the owner can open — or
  the exact reason it cannot be — and states that a loopback curl alone does not satisfy
  it. The manifest re-syncs prose and criteria on the next hourly seed.
- **Build-command timeouts.** The binding 60 s on prod was the controller's per-action
  `default` cap (`shell`/`code_execution` had no row), not the tools' own ceilings.
  `TimeoutConfig.TOOL_TIMEOUTS` gains `shell`/`code_execution` (330 s;
  `SHELL_TIMEOUT_SECONDS`/`CODE_EXEC_TIMEOUT_SECONDS`), and ONE foreground ceiling
  `SHELL_MAX_TIMEOUT_SEC` (default 300, `tools/code_exec/limits.py`) now governs both
  `shell_run` (120 when the agent omits `timeout`) and the dev-mode `run_code` cap.
- **Unreachable deliverables are named.** A completion notice whose deliverables carry
  no published URL ends with one line saying so — naming `PUBLISH_ENABLED` when the
  rail is not registered (`agents/task/goals/deliverables.py::reachability_note`).

### 031 — stop everything by prompting the agent (2026-09-03)

The owner's "stop" (text or
voice, any words) now stops every autonomous activity in seconds, survives restarts, and
every seat reports the verified state; "resume" reverses it.

- **One pause record + one predicate + a ratchet.** `core/autonomy_control.py` owns
  `<data>/AUTONOMY_PAUSE.json` (scopes, optional expiry, atomic, fail-closed); every
  autonomous starter calls `allows(kind)` (`tests/test_autonomy_control_ratchet.py`). The
  three legacy sentinels (`AUTONOMY_HALT`, `TREASURY_ENTRY_PAUSE`, `STREAM_SEEDING_PAUSE`)
  are read-only facets of it — a `touch` still works, nothing writes them any more.
- **Deterministic owner stop gate** on Telegram (text or voice, before any queue, model
  call or tool) and in the REPL: "stop" / "stop everything" / "autonomy off" pauses
  everything from the text alone; "resume"/"continue" lifts a pause; a scoped "stop
  trading" goes to the agent unless the model is credit-dead or the session is busy, in
  which case everything is paused as the safe default. The directive is recorded into
  the session as ALREADY APPLIED so a later drain can never re-apply a stale stop.
- **`autonomy_control` agent action** (owner-only; forged, leaf and non-owner turns
  refused; a correspondent-tainted session cannot reach it) so "stop trading
  for 6 hours" becomes state, not a row cancellation. The security prompt tells the agent
  to call it FIRST on any stop/pause/resume ask.
- **`/pause [scope…] [for 6h]` `/resume [scope…]`** on Telegram, the REPL and the web
  console (`/api/webgate/pause|resume`); `polyrob autonomy pause|halt|resume|status`;
  `/halt` and `polyrob owner halt|pause-entries|pause-streams` kept as aliases. One
  renderer pair produces the verified confirmation text on every seat.
- **In-flight work is cancelled on the paused edge**: goal runs return to `ready` with no
  failure increment, the running cron job returns to `scheduled`, background delegations
  of autonomous sessions are cancelled; loops stay armed (resume needs no restart); the
  a 3 s pause watcher reconciles a pause written by another process (CLI, console,
  script, touched file) the same way; the cold-start requeue stays unconditional (a
  `running` row after a restart is a lie; dispatch is gated).
- **Status leads with the pause line** on every seat (`⏸ PAUSED (…) since … by … via …`
  or `▶ RUNNING — n goal run(s), n cron run(s), loops alive x/y`); a `pause_violation`
  CRITICAL health item names autonomous activity recorded after a pause. A missing
  cron/goal store no longer blanks the loops section.
- **Out-of-process actors honour the record**: `ops_alert.py` suppresses non-`--critical`
  alerts (durably logged) while `oversight`/`all` is paused; the maint/intel watchdogs
  neither nudge nor relaunch; `dev_inject.sh` appends only; the loop prompts run a
  read-only "paused tick".
- **Fixes the pause needed to hold**: the agent's own email can never be seeded as a
  correspondent (its sent copy re-ran a finished treasury goal on every restart; a boot
  sweep expires existing self-bindings); a correspondent reply into a FINISHED session
  gets a bounded reply task instead of re-running the original goal; a paused stream
  objective is honoured, not re-created; `payload.created_by_session_id` is stamped on
  every goal so the planner's outcome accounting is honest; every self-wake row carries
  its reason.
- Deferred, recorded: the 60 s no-reply fallback for a scoped Telegram intent (§3.4), the
  `ops_alert.py --realert` counter (§3.9), the outbound-queue `held` state, multi-clause
  stop messages (a "Stop\nquestion?" goes to the agent, which calls the action).
- New pref `pause.phrases` (extra object-free words for the stop gate); new event kinds
  `autonomy_paused` / `autonomy_resumed` / `pause_violation`. Guide:
  `docs/guide/owner-controls.md`.

### "No goals" forensics — goal board, planner, streams, owner escalation (2026-08-29)

The owner received
"🫗 My goal pipeline is empty — I have nothing queued for Promote POLYROB…" twice in five
hours while the trading stream had run ten clean 3-leg cycles; the agent's own `goal_list`
showed the OLDEST 100 rows of a 409-row board and zero stream legs.

- **`goal_list` / Telegram `/goals` are newest-first VIEWS** (`GoalBoard.list_recent` /
  `status_counts`): live goals by default (`status=` for history, `limit=`), board totals
  in the header. `board.list` (priority DESC, created_at ASC) stays the dispatcher order.
- **Empty-pipeline escalation is honest** (`GoalDispatcher._maybe_escalate_empty_pipeline`):
  not a stall while work is in flight (running/waiting/triage), while a manifest stream is
  idle between cycles (`streams.next_seed_at`), or when every objective is a covered stream
  / at its budget with the spent-ask open. Names a SERVABLE objective (starved order). The
  streak and the once-per-stall marker are durable `goal_events` (`mark_planner_outcome`,
  `mark_stall_escalated`) — 14 restarts in 36 h had re-armed the in-memory flag.
- **Planner backoff** (`planner_backoff_multiplier`): cooldown ×1, ×1, ×2, ×4 (cap) after
  consecutive runs that queued nothing (read from the board via
  `count_created_by_session`, not the model's summary). Cooldown compares on the board clock.
- **Planner prompt: OPEN ASKS ground truth** — the board's open asks are listed; anything
  remembered or found in a report file that is not listed is RESOLVED, and an objective
  whose only blocker is listed is "queue healthy", not a REAL BLOCKER.
- **Stream cadence tolerance** (`CADENCE_TOLERANCE_SEC`, 15 min, ≤ cadence/4): a 4 h
  stream no longer runs at 5 h whenever the hourly timer lands seconds early.
  `stream_overdue_by` + `SEEDER_GRACE_SEC` (75 min) tell "waiting for the next tick" from
  "the seeder missed it".
- **Spent-objective ask closes itself** once the objective is no longer spent (stream
  adoption, budget raise, cancelled children) — `escalate_spent_objectives`.
- **Manifest:** the two standing missions ("Promote POLYROB … build-in-public", "Build and
  ship real software artifacts") are now streams (`promote-build-in-public`,
  `ship-software`, 24 h cadence, one live goal, no money verb), adopted by exact title.

### Layer re-evaluation S9 — goals / telemetry / cron / autonomy runtime (2026-08-29)

- **⚠ Owner-visible (B26):** a stream objective's title/body now follow `streams.yaml` while the
  row still carries the manifest prose it last followed; an in-app edit of the prose diverges
  the row and wins forever (pre-feature rows are stamped, never rewritten).
- **`GoalBoard.get` tenant filter (B25):** `get(goal_id, *, user_id=None)`; the goal tool's
  show / create / dependency reads pass the tenant, so a foreign goal id is simply not found
  (its title no longer leaks through a hand-typed dependency id).
- **Event-kind reverse contract (B27):** `tests/test_event_kind_contract.py` — every kind
  literal recorded on the durable event log must be in `core/event_kinds.py`, and every
  catalogued kind must be used in code. A free-typed kind now fails CI.
- **Dead multi-agent telemetry readers removed (B52):** `capture_multi_agent_relationship`,
  `MultiAgentRelationshipFormatter`, the `agent_graph` relationship-file branch — their only
  writer went in S7.

### Layer re-evaluation S8 — `task_agent_lite.py` + session layer (2026-08-29)

- **`TaskAgent` split (B01):** `agents/task/task_agent_chat.py` (chat front door, per-chat
  lock, model hot-swap, reply extraction), `task_agent_delivery.py` (self-wake / correspondent
  DATA / ensure-and-deliver / recreate-from-disk), `task_agent_lifecycle.py` (cleanup,
  eviction, limits, credits, lookup) and `task_agent_support.py` (`SessionRequest`, the
  strong-ref task sets, runtime resolvers). Every method verbatim; `task_agent_lite`
  re-exports the support names so importers and test seams see the same objects.
  2589 → 1096 lines.
- **In-package tests moved (B17/B53):** the 13 `test_*.py` files under `agents/` now live
  under `tests/unit/agents/...`; pytest stays in rootdir import mode (no package markers).

### Layer re-evaluation S7 — `agents/task/agent/core/` (2026-08-29)

Behaviour-preserving unless noted.

- **`_get_next_action_internal` (B02, partial):** four verbatim extractions — `_stream_with_tools`
  (the third inline `stream_with_timeout` copy), `_extract_response_content`,
  `_parse_fallback_content`, `_safe_default_output` (replaces two hand-built safe defaults).
- **`Agent.__init__` (B03, partial):** `_wire_runtime_state` hoists the self-only
  stall/session-data/debug/loop-state block; the `locals()`-consuming profile block stays.
- **Loop detection (B12):** the inline hash-repetition + A-B-A-B detector moved from `step.py`
  into `LoopDetectionMixin._record_action_for_loop_detection` (regression test).
  `MAX_REPETITIONS` / `UNCHANGED_STATE_THRESHOLD` — documented env knobs that gated nothing —
  now ARE the consumed thresholds (same defaults; identical when unset); four orphan constants gone.
- **One brain-state key set (B13):** `modules.llm.brain_scrubber.BRAIN_KEYS` (widest union,
  `immediate_step` + `phase`); `utils_json`, `task_agent_lite` and the CLI read it.
- **`MultiAgentMixin` deleted (B14):** a write-only sink whose `update_*` called a telemetry
  method that did not exist; its one live call and the orchestrator base are gone. The
  fail-open feed readers are B52 (S9).

### Layer re-evaluation S6 — money modules + `migrations/` (2026-08-29)

- **⚠ Owner-visible: a SETTLED invoice now also produces an owner notice (B10).** The
  settlement watcher used to re-enter only the originating session on settlement, while an
  expiry always pushed an owner notice too — so when the session was not wakeable the owner
  never heard that money had arrived. Settlement and expiry now ride one session-side helper
  (correspondent DATA else self-wake) and one unconditional owner notice over the durable
  delivery rail (`Invoice <id> SETTLED: $x received (tx …)`); caps/dedup apply. Regression test
  `tests/unit/modules/x402/test_settlement_owner_notice.py`.
- **`settlement_watcher.py` split (B09):** four mixins — `settlement_scan.py` (chain checks,
  Base + Solana scans, matching, sweeps + the scan helpers), `settlement_notify.py`,
  `settlement_subscriptions.py`, `settlement_reputation.py`; the watcher keeps the tick loop
  (330 lines, from 1428). Behaviour otherwise byte-identical.
- **One migration filename parser (B17):** `migrations/version_manager.py::migration_version_from_filename`
  + `shipped_migrations` replace the three hand-rolled copies in `boot.py` and the pending-list.
- **Fernet credential store moved to core:** `core/security/encryption.py` (`MCPEncryption`,
  `get_encryption`, key loading) — `tools/mcp/security.py` re-exports it for its callers; the
  three `modules/database` handlers import it downward (3 layering rows deleted).
- `FAIL_ON_INSUFFICIENT_CREDITS` gets its catalog row (the tier's one uncatalogued env read).

### Layer re-evaluation S5 — `modules/memory/`, `skills/`, `transcription/`, `pfp/` (2026-08-29)

- **`sqlite_memory_provider.py` split (B09):** the curated-notes, KB and episodes stores are
  mixins (`sqlite_curated_store.py`, `sqlite_kb_store.py`, `sqlite_episodes_store.py`) each
  owning its schema hook; `wikilinks.py` holds the notes parser. Class, MRO tail and every
  method unchanged; the local-vector subclass untouched. Ceiling 1238 → 492.
- **Two `modules→agents` edges removed:** `reflection_llm_enabled_default` is a
  `core.config_policy` accessor (exact semantics kept: an explicit empty value disables);
  `aux_metering` moved to `modules/llm/aux_metering.py` and its helpers
  (`detect_llm_provider` / `resolve_serving_provider` / `extract_token_usage`) to
  `modules/llm/usage_extract.py` — `agents/task/utils.py` re-exports them downward.
- `phase_manager.py`: a bare `except:` around the clue embedding now narrows and logs.

### Layer re-evaluation S4 — `modules/llm/` (2026-08-29)

- **`model_registry.py` split (B09):** `model_types.py` (enum, pricing/capability/config
  dataclasses, cache multipliers), `model_catalog.py` (the 77 built-in `ModelConfig` rows —
  data only), `model_pricing.py` (`calculate_cost`), `model_registry.py` (registry + helpers +
  `PROVIDER_CONFIG`); every public name is re-exported, importers unchanged.
- **`LLM_PROVIDER_REGISTRY` kill-switch REMOVED** (shipped 0.10.0 as "remove after one
  release"; two releases passed) together with the six duplicate legacy literal tables it kept
  alive (`_LEGACY_PROFILES`, `_LEGACY_PROVIDERS`, the legacy fallback hierarchy and
  `PROVIDER_CONFIG` tables, `_KNOWN_PROVIDERS`/`_PREFIX_TO_PROVIDER`, `_KEY_TO_PROVIDER`).
  Every seam derives from `provider_spec.get_specs()` and falls back to `BUILTIN_SPECS` on a
  registry fault. The flag row is gone from `docs/CONFIGURATION.md` (an env value is inert).
- **`ProviderSpec.cache_strategy`** (B17): the per-provider prompt-cache answer is a spec
  field (validated, user-settable in `providers.yaml`); `cache_hints.provider_cache_strategy`
  derives from it (OpenRouter stays model-dependent).
- **Telemetry cost estimate (B11):** `TokenCounter.estimate_cost` now forwards
  `cache_creation_tokens` (the Anthropic 1.25x cache-write surcharge was dropped on the
  display estimate). `cost_utils.py` no longer claims a delegation that does not exist.
- **Guardrails:** `modules/` joins the file-size ratchet (15 rows, shrink-only);
  provider-registry ratchet allowlist −4 stale rows (+1 for the moved enum tables).
- Deferred with a design: `DeepSeekClient` → serve `deepseek` through `OpenAICompatClient`
  after a live-key tool-call check (B16).

### Layer re-evaluation S1-leftovers + S2 config plane + S3 core surfaces/wallet/auth/security (2026-08-29)

Wave 1 complete.
All behaviour-preserving; suites green (see the review docs for the exact runs).

- **`core/config_policy/policy.py` split (F-3c map, backlog B19):** nine submodules — `_env`,
  `local_profile`, `autonomy_mode`, `autonomy_posture`, `compute_posture`, `payment_policy`,
  `capability_toggles`, `autonomy_config`, `runtime_gates` (a DAG) — behind a 112-line
  `policy.py` facade that re-exports every public and externally-referenced private name, so
  `core.config_policy.policy.X` / `from core.config_policy.policy import X` and the
  `agents/task/constants.py` shim are unaffected. Frozen state (compute posture, payment
  approval) lives with its accessors + refreeze in one submodule. File-size ratchet row
  1377 → 120.
- **Retired services with zero readers:** the Telegram-era `Permissions` (built at server start
  with four DB reads, never read; `ADMIN_IDS` field + its layering row + pin test gone) and the
  `Metrics` component; the container's write-only component-group set; `utils/__init__.py` is a
  bare package marker (its re-exports had no importer and pulled PIL on any `utils.*` import).
- **Two more core-tier relocations (B41, B44):** `SurfaceConfig` → `core/surfaces/config.py` and the
  durable telemetry event log + `emit_self_modification` → `core/event_log.py` / `core/self_events.py`.
  Both imported only core and were imported upward by a dozen core files each; every importer
  (incl. `docs/CONFIGURATION.md` anchors) now names the core module, no shims. The core→agents
  layering allowlist shrinks 34 → 12 rows over wave 1.
- **Agents-tier security shims retired (B16):** `agents/task/agent/core/untrusted_wrap.py` and
  `secret_guard.py` — every importer and monkeypatch target now names `core.security.*`.
- Five fail-open `except: pass` sites that hid a real failure now log it (`owner_notice` and
  `credit_sentinel` event writes at WARNING; pref rung / identity export / event-log prune at DEBUG).
- Adjudicated, not changed: `core/pairing.py` IS wired (dispatcher stage 0 + access + CLI — the
  08-09 "gates nothing" claim was stale); every direct `POLYROB_DATA_DIR` read is a documented
  last-resort behind `resolve_data_home` (B22 closed); the 17 inline `== "true"` parses are
  per-flag decisions for their owning tiers (B20) — `WEBVIEW_AUTH_ENABLED=1` turning auth OFF is
  the one that looks like a defect (S17); `ServerConfig` ctor IO split deferred with a design (B08).

### Layer re-evaluation S0+S1 — program tooling, `core/` foundation + `utils/` (2026-08-28)

Wave 1 of the layer re-evaluation program. Behaviour-preserving.

- **`scripts/arch_inventory.py`** (S0): AST-based, read-only inventory — per-package file/LOC
  table, import edges in/out by tier with upward edges marked, zero-live-importer modules via a
  repo-wide reverse index (test/aux importers + string refs reported separately), env reads not
  in the flags catalog, files over a size threshold. `--all`, `--json`, `--working-tree`.
- **Dead-but-plausible `core/` cluster deleted** (08-09 §2.18, every symbol verified zero live
  callers): 15 `DependencyContainer` methods incl. the authz façade (`is_admin` config fallback)
  and a `create_permissions_service` that would have raised `TypeError`; the moderator tier of
  `Permissions` (members, DB load, the `get_user_role` branch that resolved to level 0),
  `require_permission`, `get_stats`, `ServerConfig.moderator_ids` + the reader-less `roles`
  matrix; `ServerConfig._setup_logger/_log_config_details/load_twitter_config` (`self._logger`
  was never initialised); `runtime_config.env_keys_present`; `core/session_context.py`
  (Protocol with zero adopters); utils members (`result_size.should_truncate/truncate_with_notice`,
  circuit-breaker `get_circuit_breaker/force_open/get_all_stats/reset_all`, five `user_utils`
  helpers, `time_utils.parse_timestamp_to_float`, `auth_utils.get_user_wallet/get_user_role`,
  `BoundedSet`).
- **`core/surfaces/rate_bucket.py` shim removed** (08-09 §2.16) with its duplicate test; the
  stale re-export note in `core/rate_limit.py` corrected.
- **Layering ratchet:** `utils` is now ranked tier 0 (core imports it) so its upward edges are
  policed; `utils/auth_utils` no longer imports `agents.task.constants` (uses
  `core.identity.ANON_USER_ID`, the SSOT it aliased); `core/container.py → tools.filesystem`
  row removed (net shrink); `utils/gif_utils.py → agents.task.path` seeded.
- `core/knowledge_export.sanitize_filename` → `slug_stem` (it is a kebab slug for a vault
  stem, not the upload-name sanitizer in `utils/path_validator.py`).
- Docs: `core/README.md` / `utils/README.md` drift fixed (four-role matrix, phantom seam
  Protocols, deleted members). Not fixed here: the "no core→tools ratchet" claim was stale —
  R-4's `ALLOWLISTED_UPWARD_EDGES` already polices it.

### Owner questions 2026-08-29 — stray OpenRouter calls + "defi_trade always ungranted"

- **In-session LLM fallback skips a credit-dead provider**
  (`modules/llm/llm_manager.py::get_fallback_chat_model`): a keyed-but-unfunded
  provider (OpenRouter at $0) passes the `/models` health check, so the fallback
  ladder tried it for the real call and 402'd whenever the funded primary
  (zai-coding) hiccuped. It now skips any provider the credit sentinel has
  latched. With cron + goal dispatch already preferring the funded seat
  (2026-08-28) this was the last code path still reaching OpenRouter.
- **Treasury skill — the trade grant is standing, not on-demand**
  (`data/prompts/skills/treasury-trading/SKILL.md`): `defi_trade` rides the
  recurring "Treasury: manage open positions and take a screened entry" cycle
  every run. The agent was ALSO inventing its own "execute the first trade once
  `defi_trade` is granted" goals — which `goal_create` strips the money verb
  from by design — then escalating for a grant it already had; the owner granted
  it repeatedly and the loop never cleared (~50 runs). The skill now forbids
  creating that goal and forbids escalating the grant. It also kills the
  self-imposed "no entries until every position has an exit route" pause: a
  rugged/illiquid position that returns NO ROUTE on every venue is written down
  like dust and must not freeze new entries — two dead coins were holding the
  whole strategy hostage.

### Prod log forensics 2026-08-24..28 — "blind owner" / "repeats itself" (2026-08-28)

Four days of `polyrob.service` logs + goals/cron/telemetry DBs were audited.
Fixes, each with its regression test:

- **Cron provider pin** (`cron/runner.py::resolve_job_provider`): an unpinned job
  (`payload={}`) now prefers `CHAT_PROVIDER`/`DEFAULT_PROVIDER` like goal dispatch
  does. It used to resolve to the canonical-first provider — credit-dead OpenRouter
  on prod — so every status/exit-monitor tick 402'd first, fell back, and re-tripped
  the credit sentinel every 6h (1,323 402 lines, five false credit alerts).
  `provider_rerouted` is only emitted when a real stored pin was overridden.
- **`message` tool owner defaults** (`tools/controller/views.py`,
  `message_send.py::resolve_message_defaults`): `surface`/`target` are optional;
  omitted = the owner on their primary bound surface. GLM-5 omitted both 24× at the
  final notify-owner step, validation failed, and the owner never heard about a
  finished deliverable.
- **Honest rejected-tool-call error + model feedback**
  (`agents/task/agent/core/next_action_internal.py`): a call whose arguments fail
  validation is reported as exactly that (was: "these actions don't exist in
  registry"), and the reason is pushed to the model as an ephemeral message so it
  can correct the call instead of repeating it blind into a "thinking loop".
- **Goal self-wake targets the CREATING session only**
  (`agents/task/goals/dispatcher.py::_self_wake`, `goal_create` stamps
  `payload.origin_session_id`): the run session is never woken with its own result.
  126 of 134 prod wakes were "completion echo" turns (9.25M input tokens).
- **Lifecycle delivery bucket** — new `USER_DELIVERY_LIFECYCLE_DAILY_CAP` (`10`):
  `▶ goal started` / `✅ completed` / `▶ cron run started` pings get their own
  smaller daily ceiling so they cannot crowd the agent's own reports out of the
  shared 30/day cap (on 08-27 only 3 of 171 agent messages reached the owner).
- **Planner prompt**: RECENTLY DONE is labelled dedup-protected (63 of 94 planner
  runs ended in `dedup_rejected` re-proposing yesterday's work under a new suffix).
- **Step tool-call cap 3 → 5** (`step_execution.py`): 175 steps deferred 319
  actions in four days, each deferral a ~60k-token extra round trip.
- **EIP-55 refusal hint** (`core/wallet/tokens.py`): tells the model to pass the
  address all-lowercase when it is sure of it (55 refusals in four days).

### Status SSOT — no more confident-and-wrong status (2026-08-28)

The owner's Telegram `/status` rendered "Goals: 0 open, 0 running · kill switch:
clear" while OpenRouter was credit-dead (sentinel tripped 4× in 24h), 76 of 110
owner messages had been suppressed by the daily cap, two asks were open (one for
13 days), a goal was blocked on the owner and two objectives sat at their
lifetime goal budget. Seven renderers each assembled a partial view and dropped
any section whose read failed.

- **`core/status_snapshot.py`** — ONE typed, sectioned builder (session /
  providers / goals / approvals / loops / delivery / posture / money). Every
  section is always present; one that cannot be computed is
  `unavailable (<reason>)`, never a zero or a missing line. A mandatory,
  ranked **health** block leads: credit sentinel per provider (+ whether any
  provider can serve), open asks, blocked goals, pending approvals,
  budget-exhausted objectives, suppressed owner messages (cap consumption),
  loop heartbeats, kill switch, open surface circuits, tool timeouts, degraded
  runs. `OK` lists what was checked; an unreadable health source ⇒ `PARTIAL`.
  Reads the durable telemetry log (24h), tenant-scoped, no network read unless
  `include_balances`. `core/status_render.py` renders it identically on every
  seat.
- **Ported renderers**: Telegram `/status` (health first, `/mode` and `/recap`
  carry the header), `polyrob doctor` (`health:` block), `polyrob autonomy
  status` (+ `--json health`), webview `/system` (Health + Status panels via
  `/api/webgate/doctor`), the daily digest, and the agent's `agent_status`
  action (extracted to `tools/controller/agent_status_action.py`). A test pins
  that `/status` and `agent_status` render the same health block.
- **Agent self-awareness**: `LIVE_HEALTH_CONTEXT` (default ON) injects a
  `<live-health>` control note at the first step of every turn from the same
  snapshot, so "how's it going?" in prose is answered from live facts.
- **`/missed [n]`** — read the owner notices the daily cap suppressed.
- **Money labelled**: treasury renders as *cash flow (income − spend; open
  positions NOT included)*; runtime as the owner's compute bill. Never summed.
- **Session liveness** now reads the per-session execution lock (a step is
  executing), not "input is queued"; no signal ⇒ `state unknown`.
- **Fixed**: `AutonomyHandles` (the runtime the API lifespan, REPL and
  Telegram surface actually use) never emitted the `autonomy_tick` liveness
  heartbeat — prod had zero in 16k rows, so a dead loop rendered as healthy.
  `core.tickers.emit_loop_heartbeats` is now the one emitter.
- **Ratchet**: `tests/test_status_silence_ratchet.py` freezes the count of
  silent `except` handlers in status render paths (shrink-only; the SSOT
  modules pinned at 0).
- Public reader `core.credit_sentinel.credit_sentinel_status()`; the latch now
  preserves each provider's trip `reason` across expiry rewrites.

### 030 — UI & surface unification (2026-08-27)

Proposal 030 — UI & surface unification, implemented the same day.

**Security (webview)**
- The workspace preview iframe no longer voids its own sandbox
  (`allow-same-origin` dropped) — agent-authored HTML can never run with the
  owner's console origin/cookie. Authed previews keep working via scoped,
  expiring `?st=` serve-tokens. Rendered markdown is sanitized; the
  `onclick`-string XSS sink is deleted; path/status interpolations escaped.
- `/logout` works in own_ops (real redirect + cookie delete, nav link); a
  failed owner login re-mints the CSRF token instead of bricking the form.

**Delivery correctness**
- Telegram honors RetryAfter with bounded wait+retry — a rate-limited owner
  message is no longer permanently lost; flood errors never trigger the doomed
  plain-text resend.
- Construct-aware chunking: fenced code blocks close/reopen across the 4096
  boundary; bold/strike/inline-code/links never render raw; markdown tables
  degrade to `<pre>`; limits are UTF-16-aware (Telegram's meter).
- The durable outbound queue carries media (it silently dropped every photo,
  document and invoice card); media captions never duplicate the message body.
- `WebhookSurface` threads `deliver=` — WhatsApp gets failure breadcrumbs.

**One control plane, equal seats**
- The owner-address contract (`OWNER_SURFACE` + per-surface addresses):
  approval prompts, credit-sentinel halts and settlement alerts reach the
  configured surface chain — critical notices broadcast. Cron delivery and
  `message(target="owner")` reach every chat surface.
- `register_surface` enforces the surface contract; all 7 transports register;
  the correspondent registry installs centrally (was email-seat-only, so a
  telegram-only deploy DENIED every third party). Discord + Slack gain real
  media upload; their `media_out` is now truthful.
- REPL owner-verb parity: `/halt /resume /asks /fulfill /allow /deny
  /allowlist /invoices /settle` over the same core seams; `/resume` now clears
  the kill switch. Telegram: unknown/typo commands answer with help +
  suggestion (never an LLM turn), `/start` welcome, grouped `/help` +
  `/help <verb>`, `@botname` suffixes stripped.
- Approvals: grant cards (amount/target/purpose, deadline, one-shot-grant
  explainer) replace raw JSON; the non-payment owner-queue lane gets the 300s
  remote round-trip timeout (was 30s); an owner approval wakes the originating
  session (resume-on-grant).

**Config & help plane**
- The flags catalog keeps the description column — every `explain`/`search`
  surface shows real descriptions for all documented flags.
- `polyrob autonomy status|on|off|halt|resume` intent verbs (026 P3);
  `polyrob config get/list/search/explain`; `doctor --flags
  --group/--search/--changed`; one effective-posture card (all 4+1 axes,
  clamp/INERT truth) rendered by doctor, `/autonomy`, Telegram
  `/status`+`/mode` and init's closing summary.
- `polyrob session list/show/costs/export` work on a keyless box.
- Token streaming defaults ON under `POLYROB_LOCAL` (`polyrob run` live box).

**Webview**
- The eight webgate pages get a real component stylesheet (they rendered
  unstyled); nav wraps on mobile + Sessions/Chat links; ~40 silent
  catch-blocks now surface failures; silent-empty endpoints carry an explicit
  error state; reconnect is delta-sync-only; screenshot polling quiesces;
  a JS syntax gate covers the frontend; `surface list --status` shows
  per-surface health.

### Security (crypto finalization, 2026-08-27)
- **`solana_swap` gains the full guard stack its docstring promised.** The verb
  never read `max_spend_usd`, never probed the kill switch, never checked turn
  origin, never consulted PolicyGate and never recorded a spend. It now mirrors
  `tx_guard`'s step order: kill-switch → turn-origin (incl. the
  `DEFI_AUTONOMOUS_TURN_TRADING` goal lane and a `DEFI_MONITOR_EXITS`
  sell-to-USDC exit lane — Solana now has exit parity) → sell-side delta
  assertion → valuation (pinned USDC → price → exit-bounded fallback →
  measured USDC receipt) → declared ceiling in cents → PolicyGate caps under
  one reservation → autonomous `owner_queue` lane → daily-cap-required →
  pinned `DEFI_SOLANA_RPC` required to broadcast → audit record.
- **Hyperliquid refuses to arm live on the un-firewalled signer** (H4): the
  polyrob-wallet path signs with a key that fully owns its own account, so the
  approveAgent withdrawal firewall does not exist there — with
  `HYPERLIQUID_TRADING_ENABLED` set, the exchange client now refuses, naming
  H4. `agent_status` reports the actual signer. CollabLand DEBUG key-prefix
  logs trimmed 10→5 chars (H5 residual).
- **base58 survives normalization**: `_norm_tx` folds only `0x`-hex — a Solana
  settlement signature is case-significant and was being destroyed at record
  time; `record_x402_payment` now routes both addresses through the one
  chain-aware `normalize_recipient`.

### Added (2026-08-27)
- **Wallet address visibility, Solana included**: `x402_wallet_status` (all
  identities + treasury/operational split), `agent_status`
  (`wallet_addresses:` line), `polyrob wallet` (solana row + `--json` field),
  Telegram `/wallet` (Solana fund line + balances).
- **Console `/positions` page**: the agent's on-chain book — every wallet
  address, the live per-chain portfolio read, and the ledger⟷chain reconcile
  verdict — read-only over the same `defi_data` verbs the agent uses.

### Docs (2026-08-27)
- `docs/guide/payments.md` brought a feature-era forward (defi_data 9 verbs
  multichain, the 5 trade verbs + swap rail + Solana + reconcile + exit lanes,
  11 missing flags, armed-posture honesty); `security-model.md` §3(c)
  corrected (the MCP env hole is FIXED — allowlist) and §2 now names the
  actually-enforced money gates; `docs/CONFIGURATION.md` gains a dedicated
  `## DeFi / on-chain trading` section; README crypto section corrected;
  `docs/examples.md` gains a worked end-to-end money loop.

### Changed (exit untying, 2026-08-26 — closes what blocked live position closes)
- A sell of a held token to the chain's quote asset whose outflow token has no
  price by ANY source is now valued at the simulation's MEASURED quote-asset
  inflow instead of refusing — the caps run against the exact receipt. An
  exit-bounded allowance grant on such a token values at $0 (loudly) instead
  of dead-ending on "have the owner approve it by hand". Grants beyond the
  held balance still refuse.
- `DEFI_MONITOR_EXITS` (default OFF): a forged main-agent turn (self-wake /
  delegation-result — the monitor loop) may execute EXIT-shaped operations
  only: revoke, exit-bounded approve, sell-to-quote within held balance with
  a measured quote inflow. Entries and transfers stay refused on those turns.
- An approve/revoke no longer consumes the rolling daily cap (it records $0);
  the grant is still checked against headroom before it lands, and the swap
  records the real value. One ticket now costs the cap once, not twice.
- Cap arithmetic runs in cents — sub-cent quote drift ($1.9903 vs a declared
  $1.99) no longer refuses.
- A declared sell amount within 1% above the held balance clamps to the
  balance (full exits no longer fail on dust rounding / STF).

### Added
- `defi_data.reconcile(chain, ledger_path)` — compares the position ledger's
  `## Open positions` markdown table against actual on-chain holdings, in both
  directions: ledger rows the chain does not back, chain holdings the ledger
  does not explain, size mismatches, and failed reads reported as UNVERIFIED
  (unknown is never zero). Quote asset and wrapped native count as working
  capital; confidently-priced sub-$0.25 holdings classify as dust. Motivated by
  a live incident where the agent's ledger said "book flat" while three
  recorded positions sat on-chain — and the agent published the wrong side.
  The bundled `treasury-trading` skill (v5) now mandates reconcile as step 0
  of every trading run and before any public claim.

### Security (breaking for an unconfigured mainnet deployment)
- `WALLET_DAILY_CAP_USD` now defaults to **$100/24h** instead of "no cap", and
  `AGENT_WALLET_MAX_PER_TX_USD` drops from $1000 to $250. Set
  `WALLET_DAILY_CAP_USD=none` to restore the old unbounded behaviour explicitly.
  An unset daily cap used to mean NO aggregate spend bound at all — the per-tx
  ceiling is a catastrophe stop, not a budget, and cannot alone stop a
  within-ceiling loop (`x402_fetch`'s idempotency key is URL-keyed, so a loop
  mints a fresh key every iteration and the replay guard never correlates it).
- A set-but-unparseable `WALLET_DAILY_CAP_USD` now raises at load instead of
  silently meaning "no cap" (parity with the per-tx ceiling).
- A negative `WALLET_DAILY_CAP_USD`, `AGENT_WALLET_MAX_PER_TX_USD`, or
  `WALLET_VENUE_DAILY_CAP_<VENUE>_USD` now raises at load naming the key and
  value — a negative cap was never meaningful and previously parsed as a
  literal (never-satisfiable) ceiling.
- `WALLET_VENUE_DAILY_CAP_<VENUE>_USD` (per-venue caps) now raises on a
  malformed value instead of silently dropping that venue's cap — the last
  silent-cap-drop of this class in `core/wallet/config.py`. The daily cap's
  disable sentinel (`none`/`off`/`unlimited`/`disabled`) is now also accepted
  here (no-op, same effect as leaving the var unset — for consistency).
- The Finance page's "Wallet daily cap" row no longer collapses "explicitly
  disabled" (unbounded spend IS in force) and "misconfigured" (the wallet
  cannot load at all — no cap is active) into the same "unset" label; they
  now render distinctly, and neither can be mistaken for the $100 default
  being in force.

### Security
- `x402_fetch` now runs on the owner payment-approval lane (`PAYMENT_APPROVAL_TOOLS`)
  and refuses forged/autonomous turns. A payment at or below the new
  `X402_AUTONOMOUS_MAX_USD` (default $1.00) still runs unattended; above it the
  owner is asked. Previously this verb had neither gate.
- Stdio MCP servers no longer inherit the process environment (they previously
  received `AGENT_WALLET_MASTER_SEED` and every API key). Only a small
  process-launch allowlist plus values explicitly configured in
  `config/mcp_config.json` are passed. A server that relied on an ambient
  variable must now declare it in its `env` block.
- The x402 settlement replay guard (`transaction_hash_already_settled` /
  `get_payment_request_by_tx_hash` / `settle_payment_request`) now compares
  `transaction_hash` case-insensitively — normalized to lowercase at every
  store AND compare site, plus a one-time backfill migration (v1.8.0) for
  existing rows. Previously a mixed-case facilitator settlement hash could
  be re-observed by the (always-lowercase) on-chain scanner and settle a
  SECOND, unrelated same-amount invoice from the same real payment. A
  legacy pair of rows that already differ only by case is left untouched
  and logged loudly for owner reconciliation, never silently merged (M1).

### Added — self-contained x402 rail: Tier-1 no-infra receive + Tier-2 one-command endpoint

- **Treasury auto-wire from the agent wallet** (`X402_TREASURY_FROM_WALLET`,
  default ON): with `X402_PAYMENT_RECIPIENT` unset, invoices/challenges/the
  agent card/the ERC-8004 registration all use the wallet's treasury address
  via one resolver (`resolve_treasury_address()`); explicit env always wins,
  and a one-time WARN fires when both are set and differ. `polyrob doctor`
  shows the resolved treasury and its source.
- **On-chain settlement detection on `base-sepolia`**: the settlement watcher
  scan-gate accepts `base` (mainnet) and `base-sepolia` (testnet USDC contract
  + `https://sepolia.base.org`), so the full quote → invoice → pay → auto-settle
  → self-wake loop can be validated on testnet with no server and no domain.
  An unscannable chain now logs a one-time WARN instead of a silent no-op.
- **Scan RPC is operator-pinnable** (`X402_SETTLEMENT_RPC`; mainnet also honors
  `DEFI_EVM_RPC_BASE` — parity with balance reads, which the scan previously
  bypassed).
- **`X402_SETTLE_ONCHAIN_DETECT` defaults ON under effective
  `AUTONOMY_MODE=autonomous`** (receive-side, scan-only; explicit env wins).
- **Invoice results teach the delivery step**: `x402_request` now returns the
  concrete `message(...)` call (and the `media_paths` attach form when the
  invoice card renders) instead of "share these instructions".
- **Tier-2 endpoint runbook**: `scripts/setup_x402_endpoint.sh` — one owner
  command (auth preflight fail-closed, DNS check, deny-by-default nginx vhost,
  certbot, idempotent env block, `polyrob-x402-api.service` on loopback :9000,
  live 402 verification).
- Autonomy-runtime start failure in surface processes is now a WARN with
  traceback instead of a silent pass.

### Added
- **Fair goal dispatch across objectives** (`GOAL_FAIR_DISPATCH`, default ON;
  `GOAL_PER_OBJECTIVE_CAP`, default 0). The ready queue is round-robined by objective
  instead of ordered globally by `priority DESC, created_at`, so one standing objective's
  backlog can no longer take every concurrency slot. Priority still orders the first pick
  and throughput is unchanged when only one objective has ready work.
- **Declarative stream manifest** `data/streams/streams.yaml` plus `scripts/seed_streams.py`
  and the `polyrob-streams` systemd timer — N operator-granted recurring streams from one
  file on one hourly schedule, each with its own `cadence_hours` and `max_live_goals`.
  Supersedes `scripts/seed_trading_cycle.py`, which is deprecated for one release as the
  rollback path.
- `polyrob goals objective add --success-criteria --goal-budget --stream-id` and
  `polyrob goals objective show <id>`; a `/goal objective <list|pause|activate|drop>`
  subverb so a phone-only owner can steer a whole stream.

### Changed
- The goal planner is handed a computed starvation order over objectives (fewest live
  children, then oldest activity) instead of choosing which objective to serve itself.
- The planner's three numeric limits are derived from the active-objective count rather
  than hardcoded in prompt prose, and are individually pinnable:
  `GOAL_PLANNER_READY_CEILING` (default `0` = `max(5, active_objectives)`),
  `GOAL_PLANNER_GOALS_PER_RUN` (`3`), `GOAL_PLANNER_MAX_SOCIAL` (`1`). The `_maybe_plan`
  thinness gate scales with the same ceiling, so a wide board no longer has to nearly empty
  before the planner may refill it. `GOAL_PLANNER_SCALING` (default ON) reverts that whole
  planner half in one flag, the way `GOAL_FAIR_DISPATCH` does for dispatch — pinning the
  ready ceiling cannot serve as the revert, because the derived value is a `max()` of it.
- A **stream objective** (one carrying `payload.stream_id`) is exempt from the lifetime
  `OBJECTIVE_GOAL_BUDGET` (`GoalBoard.objective_budget`). That budget counts `done` children
  forever and nothing sweeps them, which is the right rail for a bounded project and fatal
  for a standing stream: a 3-leg cycle against `goal_budget: 12` jammed permanently after
  four cycles. A manifest stream stays bounded by `max_live_goals` + `cadence_hours`.
- A stream's throttles and objective lookup now use tag/kind-filtered SQL
  (`GoalBoard.stream_goals`, `GoalBoard.objectives`) instead of a
  `board.list(limit=1000)` window. The window is ordered `priority DESC`, and a manifest
  stream's rows sit below the board default priority by design, so unrelated traffic evicted
  its LIVE rows first — inverting both throttles into "seed another cycle" and making
  `ensure_objective` mint a duplicate objective every run.
- `stream_live_goals` also counts the legacy `payload.cycle` tag, so the old
  `seed_trading_cycle` timer and the new one are not blind to each other during the cutover.
- The manifest's declared objective fields (`success_criteria`, `goal_budget`, `stream_id`)
  are applied on every run, including when an existing objective is adopted — adoption used
  to stamp `stream_id` alone and discard the rest permanently.
- `seed_stream` is all-or-nothing (a mid-cycle refusal rolls back the legs already written),
  and `scripts/seed_streams.py` isolates each stream, exits non-zero on failure, and files a
  durable owner ask on the existing goal-board ask rail — an hourly timer's failures used to
  reach nobody but journald.
- `data/streams/streams.yaml` is hard-denied to every agent-writable file surface
  (`secret_guard.is_protected_config_path`), and ships in the wheel (`MANIFEST.in` +
  `[tool.setuptools.package-data]`).
- `scripts/deploy_prod.sh` now syncs `data/streams` as bundled content.

## [0.12.0] — 2026-08-21

### Fixed — session eviction killed the shared Twitter/X and MCP tools (13h prod outage)

- **Session teardown no longer destroys process-wide tool singletons.** Idle
  session eviction (`orchestrator.cleanup(full_cleanup=True)`) called every
  controller tool's private `_cleanup()` — including container singletons — so
  one session's eviction nulled `TwitterTool.client` / `MCPTool.server_manager`
  for the whole process, and the skipped `cleanup()` bookkeeping left
  `is_initialized` True, so the re-init gate never fired again (dead until
  restart; 2026-08-21, ~13h41m of failed X reads/writes plus the "anysite
  unavailable" symptom). Teardown now skips container/browser-manager-owned
  instances and releases session-owned tools via the public `cleanup()` only;
  a new ratchet test forbids cross-object `_cleanup()` calls repo-wide.
- **Twitter tool lifecycle made honest.** The credential check iterated the
  already-filtered config dict, so a deploy with missing credentials reported
  the tool enabled; it now checks the expected key set. `_cleanup()` clears the
  lifecycle flags itself; `_check_ready()` verifies the live client (a dead
  client now returns a named cause instead of an untyped tweepy
  `AttributeError` that reads like a credentials problem); `_ensure_initialized`
  self-heals a dead client and marks success instead of rebuilding the client
  (and burning a live `get_me` call) on every search/get_user/get_tweets; two
  dead init paths (`_initialize_client`, `_lazy_init`, ~70 lines, zero callers)
  removed.

### Added — named profiles: isolated, shareable bot identities

- **Profiles.** `polyrob profile create <name>` makes a fully isolated home
  under `~/.polyrob/profiles/<name>/` — its own `.env`, characters, skills,
  identity docs, memory, goals/cron state, and sessions. Select one with
  `polyrob -P <name>` / `POLYROB_PROFILE`, pin a folder to one with
  `polyrob profile adopt` (writes `./.polyrob/profile`), or make one sticky
  with `polyrob profile use`. An explicit `-P` overrides an exported
  `POLYROB_HOME`; a pin/sticky never does (one-shot mismatch warning instead),
  so servers with an explicit `POLYROB_DATA_DIR` are untouched. With no
  selection anywhere, behavior is byte-identical legacy mode.
- **Manage:** `profile list/show/path/rename/delete/alias`; `create` writes a
  `~/.local/bin/<name>` wrapper by default so `<name> run "…"` works as a
  command. `polyrob doctor` and the REPL's `/profile` print the active profile
  and both homes; `polyrob init --profile <name>` writes identity keys into
  the profile's `.env` (provider keys and the default model stay global).
- **Share:** `profile export/import` (tar.gz backup — credentials excluded,
  secret-shaped strings force-scrubbed, traversal-guarded) and
  `profile install <git-url|dir> [#ref]` / `update` / `info` (the
  `polyrob.profile.yaml` distribution format). On update, distribution-owned
  paths (characters/skills/cron/mcp.json/soul.md) are replaced; `config.yaml`
  is preserved unless `--force-config`; `.env`, `auth.json`, wallet material
  and the whole `data/` tree are never touched — a distribution ships a soul,
  never someone else's memories.
- **Daemons:** one process per profile — every surface command honours `-P`,
  and `deployment/polyrob@.service` runs `polyrob@<profile>` units with the
  homes set explicitly.
- **Guards:** file tools refuse to touch another profile's home
  (`POLYROB_ALLOW_CROSS_PROFILE=1` to bypass deliberately); a process that
  reaches the runtime without profile resolution while a sticky profile is set
  warns loudly that it would write into the default home.

### Changed — the package now ships a NEUTRAL identity (behavior change for every install)

- **A fresh install is POLYROB, not a specific person's bot.** The framework
  used to hardcode the maintainer's own character (`rob.character.json`, with
  its bio and lore) as the default persona for every install on earth, and
  `DEFAULT_INSTANCE_ID` was `"rob"`. The package now ships one neutral
  `polyrob.character.json`; `rob.character.json` and `trump.character.json`
  left the package (a specific bot's character is *data*, dropped into
  `<data_dir>/characters/` or a profile — not framework code). The default
  instance id is now `"polyrob"`.
- **Escape hatches (existing installs):** set `PERSONALITY_DEFAULT_CHARACTER`
  and/or drop your character file in `<data_dir>/characters/`; pin your
  instance id with `POLYROB_INSTANCE_ID`. A configured character name that no
  longer resolves falls back to the neutral persona with a one-shot warning —
  never a hard failure.
- **Identity docs migrate automatically.** On the default instance id, a
  one-time copy-not-move migration duplicates `identity/rob/` →
  `identity/polyrob/` in the data home (marker-gated, fail-open, source kept),
  so existing SELF/owner docs don't vanish behind the renamed directory. A
  deploy that pins `POLYROB_INSTANCE_ID=rob` explicitly is untouched.

### Fixed — the neutral identity holds everywhere (alignment sweep)

- **Every user-facing surface now speaks as the configured instance, never a
  hardcoded bot name.** The Telegram `/help` said "ROB commands" on every
  deployment; the LLM-outage notice, the `soul init` scaffold, `/v1/models`'
  `owned_by`, and `/health`'s service name carried the old name; the avatar
  generator's default seed was a person's name (now `POLYROB`, and `pfp --seed`
  defaults to the instance name, matching its own help); the dev env template
  pointed at a character file that no longer ships. All resolve the instance id
  or the neutral default now.
- **`polyrob update` on a box with the per-profile unit template silently
  no-oped.** The manual systemd steps swept the bare `polyrob@.service`
  TEMPLATE into one `&&` chain; `systemctl stop` on a bare template is invalid,
  so the chain aborted before `git pull`. Templates are now skipped; live
  `polyrob@<name>` instances are still included.
- **One character-directory precedence.** Data home > profile home > the
  shipped use-case personas (`researcher`/`coder`/`analyst`/`writer`/`ops`) >
  the packaged neutral set — one implementation, used by the CharacterManager,
  the persona resolver, and `/persona` (which now unions all tiers; the old
  cwd-relative lookup missed a profile's characters entirely).
- **Identity-card noise on a fresh install**: the banner no longer prints
  `polyrob · instance polyrob`, and `/session` / `/self` label an auto-derived
  owner instead of repeating the same name three times.

### Fixed — the agent's own work kept disappearing

- **The daily workspace GC had two owners; the read-only console was one of
  them.** `polyrob-webview.service` builds its own `TaskAgent`, and
  `TaskAgent.initialize()` unconditionally spawned `_periodic_workspace_cleanup`
  — so a monitoring console ran a destructive `rmtree` over the agent's data
  once a day. `TaskAgent` now takes `owns_workspace_gc` (default `True`, so the
  agent/API processes are unchanged) and the webview passes `False`.
- **Deploys shipped code to processes nobody restarted.** `deploy_prod.sh`
  restarted `polyrob.service` (and `polyrob-email.service`) but never the
  webview, which runs from the same `/opt/polyrob` tree. A webview process
  started 2026-08-05 therefore never picked up the 2026-08-18 project-root
  guard and kept deleting the project directory every day at 06:26 UTC for two
  weeks while `.deployed_sha` reported the fix as live. Both deploy scripts now
  restart every sibling unit, on the success and the rollback path.
- **`filesystem.read_file` corrupted every file it read.** A whole-file read ran
  through `_clean_text`, which strips each line and collapses horizontal
  whitespace — so reading a `.py` or `.yml` returned content whose indentation
  was gone, and the agent then edited from the corrupted copy. The write path
  was fixed for this in F9; the read path was missed. Reads and writes now
  round-trip. (`offset`/`limit` and `char_offset` reads were never affected.)

### Fixed — x402 could not price its own server

- **A 402 challenge carried in the response BODY is now parsed.** The client
  read only the `PAYMENT-REQUIRED` header, but the x402 spec puts the payment
  requirements in the body and POLYROB's own middleware emits exactly that
  (nothing in the codebase sets that header). `x402_quote` therefore reported
  every body-carrying server — our own gated A2A and `/v1` routes included — as
  "not a paid resource". Header challenges are unchanged; the body is a
  fallback, and a present-but-broken challenge still fails closed. Not a spend
  hole: `x402_fetch` already authorized the gate at `max_amount_usd` when the
  quote came back `None`.
- **`x402_quote` no longer requires a wallet.** Pricing costs $0; refusing it
  when `AGENT_WALLET_ENABLED` was off left invoice-only deployments unable to
  see what anything charges. A null result now says so honestly instead of
  implying "free".

### Added — x402 discovery

- **`x402_probe` and `x402_sweep`** (`tools/x402/discovery.py`): probe one
  endpoint or many, read-only, and score payability 0–5 (answered / 402 /
  parseable challenge / price disclosed / full `asset`+`network`+`payTo`
  routing) with the reasons a score fell short. Handles POST-only paywalls
  (JSON-RPC, A2A) and every `accepts` shape seen in the wild. Never sends a
  payment header, needs no wallet, bounded to 50 targets at 8 concurrent, and
  every agent-supplied URL goes through the same SSRF validator `web_fetch`
  uses. All challenge decoding delegates to the one client-side parser.

### Added — unattended treasury trading

- **Tiered on-chain spend lane (`DEFI_TIERED_SPEND_LANE`, default OFF)** — a
  goal-dispatched run can trade within the per-tx autonomous ceiling without the
  owner-queue tap. **A narrowed turn-origin bar** (`DEFI_AUTONOMOUS_TURN_TRADING`,
  default OFF) lets ONLY a goal/cron-dispatched MAIN-agent turn reach the lane;
  a leaf/sub-agent, self-wake, delegation-result, or correspondent-tainted turn
  still refuses. **Degen posture** hunts new launches (mintable/hidden-owner are
  normal for a fresh token; only a honeypot or a sell-tax above ~10% rejects).
- **Trading-doctrine skill + operator cycle seeder** (`scripts/seed_trading_cycle.py`):
  a scan→trade→publish cycle chained by `depends_on` (the trade leg never reads
  an empty watchlist), with create-time dedup that survives a retired
  near-duplicate and does not revive COMPLETED rows.
- **A position exists only if the ledger records buying it** — the portfolio's
  unvalued block is rendered as an explicit airdrop warning, not a holdings list
  (a dust airdrop is no longer published as a trade).

### Fixed — this maintenance pass (security + correctness)

- **x402 discovery SSRF/DoS**: the prober cleared a URL through the SSRF
  validator but discarded the resolved IP and re-resolved DNS on the request (a
  rebind window to cloud metadata on a funded box), and had no response-size or
  total-time bound. It now pins the validated IP (web_fetch's one rebind
  defense), caps the read at 2 MiB with a total timeout, and offloads the
  blocking DNS.
- **Unattended trading now requires an aggregate daily cap**: the autonomous
  spend lane leaned on `WALLET_DAILY_CAP_USD`, but that cap defaults to none — an
  injection could loop within-ceiling swaps to drain the treasury. tx_guard now
  refuses the autonomous (goal-dispatched) lane when no daily cap is set; an
  owner-driven trade is unaffected.
- **`profile install <url>` RCE**: the clone honored git's `ext::`/`fd::`
  transports (arbitrary command at clone time) on an attacker-controlled
  distribution string. `GIT_ALLOW_PROTOCOL` is now pinned to real transports and
  the source/ref can no longer inject a git flag.
- **Owner notices that never arrived**: an unmatched on-chain payment (money to
  reconcile) only emitted telemetry, and an empty-goal-pipeline escalation rode
  the chatter lane the daily cap can drop. Both now deliver on the critical lane.
- **First-run CLI**: an OAuth-seat-only (or keyless-provider) box was invisible
  to the key gate and crashed provider resolution (`NoneType.upper()`). The CLI
  now exports `POLYROB_LOCAL` before the gate and resolves the store-aware
  provider (or shows the canonical no-key message) instead of a traceback.
- **Artifact ledger completeness**: files BUILT by the coding tool bypassed the
  ledger, so a real deliverable failed an `artifact` acceptance check as "never
  produced"; coding writes now record, and rows key by realpath.
- **X session store**: two `FileTokenStore` instances over one `.x_session.json`
  clobbered each other, erasing a just-saved login + generated password. Writes
  are now read-modify-write.

## [0.11.0] — 2026-08-19

### Added — multi-chain DeFi

- **Chain registry SSOT (`tools/defi/chains.py`)** — Ethereum and Base as
  money-capable chains, Robinhood Chain as data-only (capability decided by
  on-chain evidence, not vibes); the tool door checks chain capability while
  the transaction guard keeps its own RPC pin. Per-chain portfolio views, a
  provider-id price filter, and a registry-driven chain gate on every money
  verb. Gas is sized from the simulation's `gasUsed` (a fixed 120k limit
  would out-of-gas a real swap).

### Fixed — first-run install & CLI UX (proposal 027, clean-room verified)

- **A plain `pip install polyrob` can actually run tasks.** Module-scope
  playwright imports sat on the task-agent import chain, so a core install
  (no `[browser]` extra) died with a bare "Task package not available" —
  including the wheel published on PyPI. Browser modules are now import-safe;
  the hard failure moves to launch time with the pip remedy.
- **The wheel ships `migrations/`** (it was omitted — two causes: missing
  `__init__.py` and missing from `packages.find`), and the CLI container now
  runs boot migrations, so `polyrob run` can no longer hit `no such column`
  after an upgrade. `polyrob update`'s pip/pipx steps state the auto-migrate
  contract instead of prescribing a command that could not work.
- **Honest exits + actionable failures**: `polyrob run` exits 1 when the
  session fails and prints one remedy line (auth / billing / pip extra); a
  bad key halts on the FIRST attempt (the retry classifier's bare `"rate"`
  substring matched "geneRATE", so every provider error retried with
  backoff); mid-run tracebacks squelch to one line unless `--verbose`;
  `serve`/`dashboard`/`telegram`/`whatsapp` preflight their extras BEFORE
  side effects instead of crashing with raw tracebacks.
- **One credential path**: a single-provider connect prompt in `polyrob init`
  and the inline wizard (was six sequential prompts accepting fake keys
  silently), one no-key remedy grammar everywhere (`polyrob auth add` first),
  `config set <secret>` writes global scope by default (`--project` opts
  out), a rejected live-probe key offers removal, OAuth seats are usable
  after connect under local mode (`LLM_AUTH_STORE_ENABLED` local default ON),
  zero-key resolution returns nothing instead of inventing `gemini`, and new
  `/auth` + `/doctor` REPL slashes.
- **Directory hygiene**: read-only commands (`doctor`, `--help`) no longer
  write `.polyrob/logs/` into the CWD (log dirs create lazily on first
  write); the REPL creates its session dir only after the key gate; running
  from `$HOME` no longer mixes the data home into `~/.polyrob`; `init` writes
  `.gitignore` only inside git work trees; telemetry defaults off.
- **Interface**: grouped `--help` (Start here / Surfaces / … instead of 40+
  flat rows), aliases collapse onto their canonical row, `--json` on
  `doctor` / `model list` / `auth status`, frozen-flag INERT disagreements
  surface in plain `doctor`, `kb export` = the knowledge-vault export.
- **Docs truth pass**: one canonical quickstart (the pipx playwright step now
  targets polyrob's venv), `POLYROB_LOCAL` vs `AUTONOMY_ENABLED` untangled,
  CLI memory-backend default corrected, `polyrob auth` documented, the six
  shipped OAuth seats acknowledged, `install.sh` adopted + history-safe.
- **Guard rails**: new `tests/install/` clean-room suite (wheel packaging,
  extras import matrix, exit codes, no-pollution) + a bare-venv wheel gate in
  the release process.

### Added

- **Artifact ledger (`core/artifacts.py`)** — one durable row per file the agent
  produces (producer, path, sha256, size, kind, published url), written at the
  single filesystem write choke point and attributed to a goal by the dispatcher
  BEFORE any exit branch, so a run that failed on `max_steps` keeps the evidence
  it produced. `verify()` returns `ok`/`changed`/`missing`/`unknown`, which lets
  the new `artifact` acceptance check tell "never produced" from "produced then
  deleted". Tenant scoping is structural. New sidecar `artifacts.db`.
- **Ship rail (`core/publish.py`, `tools/publish/`, `PUBLISH_ENABLED`, default
  OFF)** — the agent can put a built page at a real URL: `publish` /
  `publish_list` / `unpublish`. A FIRST publish of a NEW slug is gated by a real
  approving provider (same resolution as `hf_deploy`, including the
  `auto_notify`→`owner_queue` remap); the same slug then iterates unattended.
  The approval gate is enforced by the FILESYSTEM LAYOUT — an unapproved
  publication waits under `<PUBLISH_ROOT>/.pending/`, which is not a valid slug —
  so the web server needs no application logic. Refused for leaf/sub-agent and
  forged (self-wake / delegation-result) turns, confined to the session
  workspace, credential files refused. Serving side:
  `deployment/nginx/polyrob-publish.conf` + `scripts/setup_publish_vhost.sh`
  (owner-run) serve `/<slug>/` statically and proxy `/api/<slug>/` to the dev
  container's loopback port. New sidecar `publications.db`.
- **`artifact` acceptance check** — `{"type":"artifact","name"|"id",…}` resolves
  through the ledger instead of a workspace-relative path, so a wipe or a
  relative-path mismatch can no longer masquerade as "never produced".
- **Retry continuity** — the goal run task now also carries what earlier attempts
  PRODUCED (verified against the ledger), not only what failed.

- **Agent mail by default (AgentMail provider)** — the agent can now have its
  OWN email address with one env var. `EMAIL_PROVIDER` (`auto`|`smtp`|
  `agentmail`) selects the transport behind the unchanged `email` tool/surface:
  with `AGENTMAIL_API_KEY` set, the agent idempotently provisions a managed
  inbox (api.agentmail.to) on first run — no SMTP/IMAP setup — and sends/
  receives from it (address persisted to `<data_home>/agent_mail.json`, minted
  RFC Message-IDs + a thread map keep correspondent reply-routing exact). New
  identity primitive `core/instance.py::resolve_agent_email` (sender identity,
  distinct from `POLYROB_OWNER_EMAIL`); `POLYROB_AGENT_EMAIL` overrides. The
  legacy GMAIL_* smtp path is byte-identical; receive rides a new
  `MailFetcher` seam (`surfaces/email/fetchers.py`).
- **X (x.com) browser rail (`tools/x_browser/`, `X_BROWSER_ENABLED`, default
  OFF)** — the agent can register its own X account and post from it through a
  real browser on a durable, encrypted login. New `x_browser` tool with
  dedicated, approval-gated verbs `x_post` / `x_login_check` / `x_signup_start`
  (`high_impact` + `delegate_blocked`; `x_post` owner-approval-gated,
  `x_signup_start` always owner-queued). Signup is a deterministic state machine
  that pulls the verification code from the agent's own inbox, stores a generated
  password encrypted before typing it, writes an automation disclosure to the
  bio, and escalates every CAPTCHA / phone check / unknown page to the owner
  (`SignupPaused` → notice + goal-board ask; headed waits for an in-window solve,
  headless pauses with a resume command). Browser login persistence via
  `BrowserContextConfig.storage_state`. CLI `polyrob x-account
  capture-session` / `status` / `signup [--resume]`. No CAPTCHA solving, no
  anti-detection changes, one account per instance.

### Changed

- **Owner asks and approvals ride a lane the daily cap cannot drop.**
  `_CRITICAL_SOURCES` now covers `approval`, `payment_approval` and
  `goal_blocked`; `push_owner_message` takes a `source`, so a blocked-goal
  escalation stops sharing the chatter source. Prod delivered 1 of 196 owner
  notices in 8 days, six of the suppressed being owner APPROVAL requests.
- **An ask closes when the goal it blocks no longer needs the owner** — new
  `ASK_OBSOLETE` status (deliberately distinct from `fulfilled`, which claims the
  owner acted), released by `record_success`/`cancel`.
- **A provider pin is a preference, not a death pact** —
  `core.runtime_config.resolve_live_provider` re-routes a durable cron pin or a
  goal pin whose provider is credit-dead, and goal dispatch pauses only when
  NOTHING can serve rather than when the default provider is dead.
- **An objective may be standing, but not infinite** — `OBJECTIVE_GOAL_BUDGET`
  (default 25 live children) refuses a further child and names the alternative;
  the planner sees each objective's spend.
- **A published deliverable is reported by its URL** instead of "attached" or
  "server-only: <path>".
- **`.env.example` and friends are writable again.** The `.env*` credential glob
  swallowed env TEMPLATES, blocking the agent from producing a deploy package.
  Scoped to `is_credential_file` only — `is_secret_path` (ingestion into model
  context) still refuses them.

### Fixed

- **The DeFi money verbs joined the approval lane and the taint gate.** Three
  trading verbs shipped on NO approval lane and outside the correspondent-taint
  gate's name layer — an approval-mode deployment could reach them without the
  owner's OK. Every money verb now rides the same approval + taint + cap
  ladder, enforced by an end-to-end real-guard suite.
- **Usage is billed to the SERVING provider, not the model's vendor** — a
  Kimi model served through OpenRouter was attributed (and priced) as
  Moonshot; and a flat-rate subscription seat no longer fabricates per-token
  spend in the aux + display paths.
- **The daily workspace cleanup deleted the agent's home.** Under
  `POLYROB_PROJECT_DIR` every session's workspace IS the shared project root, so
  `cleanup_old_workspaces` called `shutil.rmtree` on it once per old session
  ("removed 198/202 old workspaces", 2026-08-16/17) — destroying a week of
  artifacts and breaking the goal acceptance checks, which then failed
  "file not found" on evidence a previous round had really written. A per-session
  workspace stays collectable; a shared project root is not scratch.

- **Provider-outage resilience wave (2026-08-16 log review)** — the 08-13..16
  z.ai/OpenRouter double outage ground ~12k journal errors and 63 dead
  sessions/day because quota death wore a 429 costume:
  - `translate_llm_error` classifies plan-quota exhaustion ("limit exhausted",
    "insufficient balance" — z.ai codes 1310/1113) as `LLMPermanentError`, not
    a retryable rate limit.
  - The credit sentinel matches those shapes, and a provider-stated reset time
    ("…will reset at 2026-08-18 18:01:49") now latches until that reset
    (`extract_reset_ts` + per-entry `release_ts`) instead of re-tripping — and
    re-pinging the owner — every `CREDIT_SENTINEL_RELEASE_HOURS` window.
  - `AnthropicClient._generate_with_tools` no longer retries WITHOUT tools on
    rate-limit/billing/connection errors (only on request-shape errors): the
    blind fallback doubled the hammering and returned zero tool_calls — the
    "empty action list" / thinking-loop CRITICAL storms.
  - Autonomous (goal/cron) runs release their persistent shell sandbox
    container at run end — partial cleanup kept them alive until the next
    restart (7 leaked `polyrob-sbx-*` containers found).
  - `SessionStatus` gains `INITIALIZING` (247× "Invalid status value" warnings).
  - New `scripts/vps_maint_watchdog.sh` (systemd timer): the on-VPS maintenance
    loop is supervised — relaunched when its tmux session dies, nudged when the
    pane stalls, owner-alerted once (without flapping) when auth-blocked.
  - Backlog wave (2026-08-17): a session whose serving model has no vision
    support never captures/attaches screenshots (`_resolve_session_vision` —
    the per-call strip missed images already in history, ~1.8k wasted adapter
    replacements); the non-tool Anthropic path converts role='tool' messages
    to labeled `[tool result]` user text (744× "Unknown role" warns); the
    provider-fallback exclusion list is deduped; telegram outbound targets are
    normalized at the API boundary (`t.me/x`/bare username → `@x`; raw form
    kept for allowlist matching) and `@…bot` recipients are refused pre-send
    with a `target='owner'` remedy; the consecutive-failure halt no longer
    logs a stray "NoneType: None"; `vps_maint_watchdog.sh install` also
    installs a weekly age-filtered docker-hygiene timer.

- **Telegram replies arrived unformatted, on every model (2026-08-17)** — both
  outbound paths were incapable of rendering formatting, so the LLM only set how
  much raw markdown syntax the owner saw:
  - The surface path escaped EVERY MarkdownV2 reserved char one character at a
    time and still shipped `parse_mode="MarkdownV2"`, so Telegram rendered the
    markers literally (`**bold**` arrived as asterisks, `$0.42` as `$0\.42`).
    The per-character call also defeated the escaper's own code-fence branch.
  - The harness path (post-run deliver + the cron `TelegramBotSink`) sent with
    no `parse_mode` at all — raw markdown syntax.
  - Fix: one converter, `core/surfaces/rendering.py::markdown_to_html`
    (markdown → the HTML subset Telegram renders), reached through
    `TelegramSurface.send_text()` — the single seam all three paths now use. It
    retries a chunk as the original markdown if Telegram rejects the markup, so
    a converter edge case degrades formatting instead of dropping the message.
    `markdown_flavor` is `"html"` (it was `"markdownv2"`, which never matched
    `render_for_flavor`'s `"markdown_v2"`, so the shared renderer had been
    silently skipping Telegram).
  - Dedup: this cluster held four splitters and two escapers. Deleted
    `utils/markdown_utils.py` + `utils/message_utils.py` (their only live
    consumer, `SystemPromptManager.display_prompts`, was itself dead and already
    raising `TypeError` on a wrong call signature) and
    `harness._split_for_telegram`; `core.surfaces.surface.split_message` now
    aliases the one `rendering.split_text`.

### Added

- **Context-usage audit wave (P1–P8)** — a fresh REPL session
  on glm-5 read `ctx 43%` before the first user message; root causes fixed:
  - The input budget caps its output reserve at `COMPLETION_RESERVE_TOKENS`
    (default 16384; `0` = legacy full `max_completion_tokens` reserve). glm-5's
    budget goes 61,542 → 176,230; five registry rows that clamped to the
    1,000-token floor (reserve==window) become usable.
  - Registry data fix for those five rows (kimi-k2, kimi-k2-0905, qwen3-coder,
    deepseek-speciale, minimax-m2) with a ratchet test: no row may declare
    `max_completion_tokens` ≥ 95% of its window.
  - The ctx gauge counts what is actually sent: the environment and
    tool-catalog foundation blocks join every sum; the emitted tool-schema list
    (`tools` param, ~7.5K tokens on the default rig) is stamped from the step
    loop and included behind `CTX_COUNT_TOOL_SCHEMAS` (default on); the H-MEM
    injection is counted with the real tokenizer instead of `words*1.3`
    (measured 0.58–0.77× real).
  - `/context` gains environment, tool-catalog, tool-schema, and last-known
    H-MEM rows.
  - Anthropic-compat observability: a one-time WARN when a response has output
    usage but no input usage (z.ai recorded `prompt_tokens: 0` silently), and
    `provider_cache_strategy` reports `in_client` for
    `ANTHROPIC_MESSAGES`-transport rows (they inherit the `cache_control`
    breakpoints; previously mislabeled `none`).
  - The development tree now carries a compact `polyrob.md` (~1.1K tokens) that wins the
    project-context precedence over the ~20K-token `AGENTS.md` auto-load.

- **Flag configurability P0+P1 wave (proposal 026)** — writes take effect and
  diagnosis stops lying:
  - `load_env` re-freezes the import-frozen policy flags
    (`AGENT_COMPUTE_POSTURE`, `PAYMENT_APPROVAL_MODE`,
    `APPROVAL_TIMEOUT_SEC`/`APPROVAL_GRANT_TTL_HOURS`) exactly once per
    process after env-file layering — `polyrob config set` for these flags
    was a permanent silent no-op on every CLI path; a mid-session env
    mutation still cannot move them.
  - Every bare CLI command group (`cron`, `goals`, `tools`, `approvals`,
    `skills`, `soul`, `subagents`, `surface`, `todos`, `pfp`, `skill`,
    `journey`, `update`, `config`) now loads the env-file ladder via one
    memoized seam before reading flags (shrink-only ratchet test).
  - Surface launchers (`_surface_runner`, `gateway`) setdefault their
    convenience flags AFTER the preflight's `load_env`, so a file-set
    `false` wins.
  - Enum-shaped flags (`AUTONOMY_MODE`, `AUTONOMY_POSTURE`,
    `AGENT_COMPUTE_POSTURE`, `PAYMENT_APPROVAL_MODE`, `OUTBOUND_POLICY`,
    `MEMORY_BACKEND`, `CODE_EXEC_BACKEND`, `TOOL_SCHEMA_ERROR_POLICY`) reject
    typos with the valid set on all three writers + `config check`
    (`core/config_policy/flag_enums.py` SSOT).
  - Post-write honesty notes on every writer: project-shadows-global,
    process-env divergence, the `AUTONOMY_MODE=autonomous` clamp echo (names
    the missing owner-binding prerequisite at write time), and the
    server-ladder note for remote surfaces.
  - `doctor --flags`: import-frozen flags report the FROZEN value (a
    differing env value is marked INERT); the 8 `AUTONOMY_MODE` capability
    flags resolve through the mode with an honest `default(mode:…)` label;
    the clamp header appears in the flags view; a backtick-quoted numeric
    catalog default (`` `0` ``) no longer mis-kinds as bool.
  - `polyrob goals create/list`, `owner invoices`, `owner sub *` warn (never
    block) when their loop flag is off, with the config-set remedy; REPL
    `/autonomy` errors on arguments instead of ignoring them and gains
    mode/posture/cron/halt rows.
- **Server project-context walk confined to the tenant workspace (P1-8)** —
  with `PROJECT_CONTEXT_SERVER_MODE` the context-file search can no longer
  ascend from the session workspace to a surrounding deployment git root
  (which leaked the install's own `AGENTS.md`); the secret-path guard now
  evaluates candidates relative to the search root so a workspace under
  `data/…` can load its own file.

- **`polyrob auth add` now connects key-based providers**, not just OAuth
  rows: a provider row with an env key and no OAuth flow gets a guided
  connect — signup URL and terms note shown, hidden key prompt, key written
  to `~/.polyrob/.env` (0600), the `doctor` readiness line echoed, and an
  optional live validation against the row's real endpoint
  (`--validate/--no-validate`; a 401/403 warns immediately with the scoped
  `config unset` remedy instead of surfacing at the first real run). For the
  z.ai pair it asks WHICH plan the key belongs to and routes `ZAI_API_KEY`
  (GLM Coding Plan seat) vs `GLM_API_KEY` (pay-as-you-go) — the same key
  string works only on its own endpoint. `polyrob init`'s deferred-provider
  footer and `doctor`'s "not configured" roll-up now point at the verb.
- **`polyrob config migrate`** — explicit, one-time migration of secret keys
  from the legacy env files (root `.env`, `config/.env.production`,
  `config/.env.development`) into `~/.polyrob/.env`. Lists candidates by NAME
  only (values never shown), per-key confirmation (`--all` for scripts),
  idempotent, and it never imports flags (suffix selector + flag-value
  filter). Replaces the retired automatic backfill below.
- **`polyrob doctor` names the source-file tier on every env-backed
  credential line** (`~/.polyrob/.env`, `./.polyrob/.env`, root `.env`,
  `config/.env.*`, or `process env (overrides <file>)`), and the malformed-
  credential remedy now carries the matching scope (`--global` for the home
  file; "remove it from `<file>`" for tiers `config unset` does not manage).
  A shadowed key — a bad value in a higher tier masking a working one below —
  is now visible in one read.
- **`polyrob config path` derives from the env-layering SSOT**: names what
  each tier is for, lists legacy tiers only when the file exists, and calls
  out a relic `config/.env.*` file the resolved environment does not even
  read, with the migrate remedy.

- **`polyrob config unset KEY`** — the counterpart `config set` never had:
  removes a key from the env file (project scope by default, `--global` for
  `~/.polyrob/.env`), with the same whitespace-normalized matching `set` uses.
  Until now a stale or malformed credential could only be cleared by
  hand-editing the file. When the key lives in the *other* scope, the error
  names the exact command to run instead of a bare miss. `polyrob doctor` now
  names this verb directly on a "present but unusable — malformed" credential
  line.

- **Subscription plans that issue an API key now ship as built-in providers**
  (proposal 024 T0): `ollama-cloud` (Ollama Cloud, `OLLAMA_API_KEY`),
  `zai-coding` (z.ai GLM Coding Plan, `ZAI_API_KEY`) and `cerebras`
  (`CEREBRAS_API_KEY`). Previously a subscriber had to hand-author a
  `providers.yaml` row to use the plan they were already paying for. The rows
  are appended after the six original providers and are excluded from automatic
  failover, so they cannot change which provider an existing install resolves
  to, and they stay inert until their key is set. `polyrob init` names them
  once instead of adding three prompts nobody without the plan can answer
  (new per-row `prompt_in_init:`). ⚠ Declared from vendor documentation and
  **not** live-verified against each plan (proposal 024 §8) — a wrong model id
  or endpoint surfaces as a provider 4xx, never a wrong answer.
  `ollama-cloud` is the **hosted** product and is deliberately distinct from a
  local Ollama, which stays a keyless `providers.yaml` row on loopback.
- Claude Pro/Max and ChatGPT Plus connect via the OAuth flows below
  (`polyrob auth add anthropic-oauth` / `openai-codex`) — they issue no API
  key, so the key-based connect does not apply to them.
- **`polyrob config set KEY` now prompts for VALUE** instead of requiring it as
  an argument — hidden input for a secret-shaped key, and one line read from
  stdin when it is piped. Passing a credential as argv put it in shell history
  and in `ps` output for the life of the process; that form still works
  unchanged. A blank value is refused rather than written (an empty credential
  reads as "configured" at every presence-only gate). Previously the only
  interactive key entry lived inside the full `polyrob init` wizard, which also
  re-asks about model, persona, autonomy and wallet — unusable on a box that is
  already set up.

- **Credential-aware provider oracles (proposal 024 L1.5)** —
  `credential_status()`, `providers_with_credentials()` and
  `usable_providers_with_credentials()` in `modules/llm/profiles.py`, delegating
  to the one resolution oracle. Every surface that asks "do we have a
  provider?" — `doctor`, `model list`, the banner, `serve`/`dashboard` gates,
  `init`, the env backfill, the runtime resolver, the webview console — now
  routes through them, so a store-backed credential is visible instead of being
  reported as "missing" while it serves requests. With `LLM_AUTH_STORE_ENABLED`
  off (the default) they return exactly what the key-only oracles returned,
  which is what made the migration a no-op for existing installs.
  `polyrob doctor`'s block is now "provider credentials", naming the source and
  expiry for a connected account and saying WHY a configured provider is
  unusable (malformed / expired / rate-limited / exhausted) instead of
  collapsing all four to "malformed".

- **OAuth connect flows + `polyrob auth` (proposal 024 L2)** — device code
  (RFC 8628, the default; the only flow that works on a headless server) and
  loopback PKCE (refused unless `POLYROB_LOCAL` — a server must never open a
  redirect listener). `polyrob auth add/list/status/remove/refresh`, a
  per-provider `refresh_skew_sec`, and refresh serialized with an identity
  guard so a concurrent caller cannot burn a second single-use refresh token.
  Gate `LLM_OAUTH_ENABLED`, **default OFF everywhere including local** —
  connecting a subscription seat is a deliberate act with a terms-of-service
  dimension, so local mode never switches it on as a side effect.

  **No built-in OAuth providers ship.** Claude Pro/Max and ChatGPT Plus issue no
  OAuth `client_id` to third-party applications; the only ids that work are the
  ones embedded in those vendors' own CLIs, and using one would make POLYROB
  present itself as their product. Declare an `oauth:` block in
  `~/.polyrob/providers.yaml` with a `client_id` you are entitled to use — both
  endpoints must be `https` and are validated exactly like `base_url`.
- A token is never printable: `Credential.redacted()` is the only display form,
  `FlowResult` redacts in `repr`, and OAuth error bodies are scrubbed before
  they reach a user (an error response can echo the token that failed).

### Changed

- **The automatic env-key backfill is retired** (`POLYROB_ENV_KEY_BACKFILL`
  default ON → OFF; the flag and the helper are deleted next release). It
  silently imported dead production keys on a zero-key boot and vanished them
  again the moment one real key was set. When the old path would have fired,
  a one-time WARN names `polyrob config migrate`; setting the flag truthy
  keeps the legacy behavior for this one release.
- `python main.py` / `polyrob serve` pre-load env files through the one
  layering SSOT (`core.bootstrap.load_env`) instead of a divergent
  single-file read that ignored `CONFIG_ENV` and the layering order.

### Fixed

- **Surface-spawned sessions no longer hardcode openai/gpt-5 — every inbound
  telegram turn died on a pinned-provider box (2026-08-14 prod outage).** A
  telegram message spawns its session via `create_session(request=<text>)`,
  which inherited `SessionRequest`'s bare `provider="openai"`/`model="gpt-5"`
  literals; on a box with no OpenAI key the turn then had to survive the
  fallback hierarchy, and with OpenRouter out of credits it found nothing —
  voice and text turns alike failed with "no fallback providers could be
  initialized" while the operator-pinned `zai-coding` seat sat healthy.
  `SessionRequest` now resolves missing provider/model through the shared
  runtime-config ladder (`resolve_session_runtime` in `core/runtime_config.py`:
  explicit > `CHAT_PROVIDER`/`DEFAULT_PROVIDER` pin with `DEFAULT_MODEL` >
  first keyed provider > openai/gpt-5 last resort), the same precedence
  goals/cron dispatch and `chat_once` already use; the HTTP API and A2A
  session paths ride the same resolution when the caller omits model/provider.
  `TaskSessionConfig`/`LLMConfigModel` resolve the same way (a `model_validator`
  fills provider+model as a PAIR, so pinning one side can never pair a foreign
  model with a pinned endpoint) — the literals are gone from all four sources,
  and `agents/task/config.py::resolve_session_provider_model` is the single
  agents-tier resolver the other sites delegate to. A model-only caller gets
  that model's own provider from the registry rather than the operator pin.
- **`CHAT_MODEL` was silently dropped when resolving a session.** The pin pair
  is now read as a unit (`CHAT_MODEL` > `DEFAULT_MODEL`, mirroring
  `operator_provider_pin`'s `CHAT_PROVIDER` > `DEFAULT_PROVIDER`); an operator
  who pinned `CHAT_PROVIDER`+`CHAT_MODEL` previously got the registry default
  model for that provider instead of the model they pinned.
- **A primary client named `openai_fallback_client` resolved to the bogus
  provider `openai_fallback`** in the fallback walk (suffix-only `_client`
  strip), so it had no `llm_config` entry, built no isolated client, and was
  silently skipped. One `_provider_of()` derivation now matches the two-step
  strip the rest of the manager uses.
- **`get_fallback_chat_model` now tries the (operator-pinned) primary client
  before the generic hierarchy.** A pinned subscription seat (e.g.
  `zai-coding`) is deliberately `fallback_eligible=False`, so it never
  appeared in `FALLBACK_HIERARCHY` — a persisted session that requested a
  keyless provider could exhaust the hierarchy and die while the deployment's
  actual serving client was healthy. Exclusions still apply, so the provider
  that just failed is never retried.
- **`faster-whisper` is declared in `requirements.txt`** (was only the
  pyproject `voice` extra): the prod venv rebuild dropped it and telegram
  voice notes routed as empty text until it was hand-installed. The deploy's
  dep sync now keeps transcription installed.
- **A provider ALIAS (`-p glm`, `DEFAULT_PROVIDER=kimi`) died as "No client
  found for provider glm" even with a valid key set.** The CLI's known-provider
  validation accepted spec aliases, but nothing canonicalized them to the spec
  name the LLM manager registers clients under. `resolve_runtime_config` (the
  one resolver both surfaces share) now canonicalizes explicit, pinned, and
  CLI-stored providers via the new `canonicalize_provider` (name-then-alias,
  same shadowing rule as `get_spec`); unknown names still pass through as typed
  and error honestly at the manager.
- Opaque OAuth tokens (`gho_`, `github_pat_`, `sbp_`-style) were neither
  JWT-shaped nor `sk-`-prefixed, so **every** rule in the shared secret-shape
  battery missed them and they could land verbatim in a log line or a persisted
  transcript. Added as a shape anchored on an unbroken 24+ alphanumeric run, so
  it cannot swallow ordinary snake_case identifiers or paths.
- `get_spec()` matched aliases in table order, so an earlier row claiming a
  later row's NAME as an alias silently hijacked it — shipping `zai` alongside
  `zai-coding` (which aliased `zai`) handed an OpenAI-compatible endpoint the
  Anthropic schema generator, which would have shipped tools it cannot parse.
  Exact name now wins over any alias, and four whole-table invariants are
  pinned (no alias shadows a name, no env var is claimed twice, every row
  routes to a real schema generator matching its transport, every default_model
  is one its row declares).
- `BotConfig.available_providers()` only scanned the six pydantic `*_api_key`
  fields, so it reported a `providers.yaml` row, a shipped subscription row, or
  a store credential as absent even while the agent was running on it. It now
  unions that scan with the credential oracle.
- **`ProviderSpec.subscription` was declared and read by nothing** — a flat-rate
  plan was billed per token in `usage_records` exactly as if it were metered.
  It now drives the billing entry point (`compute_llm_cost(..., provider=)`):
  a flat-rate provider records $0 marginal API cost, because multiplying tokens
  by a price describes no charge the operator actually incurs. Fails open to
  metered for an unknown provider, so the error direction can only ever
  overstate cost. Cerebras stays metered by default (one endpoint serves both
  Code Pro/Max and pay-per-token); a Code subscriber opts in with
  `subscription: true`.
- A built-in provider served by the generic transport clients got no entry in
  `BotConfig.get_llm_config()`, so `LLMManager` skipped it at bootstrap — the
  provider looked configured and was silently unusable. `extra_llm_config_blocks`
  now synthesizes the block for any spec without a hand-written literal one.
- `providers.yaml` no longer warns "transport cannot be overridden" for a row
  that merely **restates** a built-in's own transport. Only a genuine change is
  refused — the warning was firing on files that were exactly correct.

## [0.10.0] — 2026-08-11

Two new capability surfaces — user-declared LLM providers and on-chain token
operations — on top of a large correctness and honesty pass across the agent
loop, billing, security gates, and the provider/credential UX.

### Added

- **On-chain token sight (`defi_data`, `DEFI_DATA_ENABLED`, default off):** a
  read-only tool giving the agent eyes on Base — `token_resolve` (ranked
  candidate contracts for a ticker), `token_info` (on-chain identity + price +
  liquidity + a safety screen), `price`, `portfolio` (own holdings, USD-valued)
  and `contract_read` (raw `eth_call`). No signer is constructed and nothing is
  broadcast, so it cannot move value. Two rules run through the whole tier:
  **an address is the only identity** — `token_info`/`price`/`contract_read`
  reject a ticker outright, and `token_resolve` returns every candidate and
  never picks, because binding a symbol to a contract is the primary injection
  surface for an agent that reads web pages; and **unknown is never zero** — an
  unreadable price renders `unknown`, an unreachable safety screen renders
  `UNSCREENED` (never "safe"), a balance that failed to read is listed
  separately as unknown, and a partial portfolio scan says outright which
  addresses it looked at and that anything outside that set is invisible. Only
  high-confidence prices enter a portfolio total, so an attacker-seeded pool
  cannot inflate the headline figure an owner reads. Token metadata is pinned
  for verified tokens and frozen on first sight otherwise, with any later
  divergence surfaced as `metadata_changed` rather than accepted. Results are
  untrusted-wrapped (a token's `name`/`symbol` are chosen by whoever deployed
  the contract), and `portfolio` alone is gated while a session is
  correspondent-tainted. An `ALCHEMY_API_KEY` upgrades holdings enumeration
  from an honest partial scan to a complete index; it is never required.
- **On-chain transfers behind a transaction guard (`defi_trade`,
  `DEFI_TRADE_ENABLED`, default off):** the first verb that can move real
  funds — `transfer`, with `dry_run=true` by default. Every call routes through
  a single choke point (`core/wallet/tx_guard.py`) that **simulates the
  transaction, measures its observed asset and allowance deltas, and asserts
  them against a declared intent — refusing on any disagreement or any probe
  failure.** The bound is not "we only wrote safe verbs", which stops being a
  security property the moment the agent supplies its own calldata. Nine
  ordered, fail-closed gates run before anything is signed: owner kill-switch,
  turn origin (a forged, self-wake, delegation-result, delegated-leaf or
  autonomous turn can never reach a money verb, and an unprovable origin
  refuses), structural checks, an RPC-trust gate that refuses to arm on the
  shared public endpoint, simulation trustworthiness, the delta assertion (an
  *undeclared* allowance grant refuses outright — a hidden approve is the one
  effect a USD cap cannot bound, because the drain happens in a later
  transaction), pricing (an unpriceable outflow refuses), the per-transaction
  ceiling plus rolling daily caps and replay guard, and finally the approval
  lane: above `DEFI_AUTONOMOUS_MAX_USD` (default `$25`) the call returns
  `lane=owner_queue` and does **not** execute. Result rendering is honest by
  construction — a refusal says NOT SENT, a reverted receipt says the transfer
  did not happen but gas was spent, and a receipt that never arrived says
  BROADCAST BUT NOT CONFIRMED and warns against blind retry. The signing
  perimeter is deliberately narrow: transaction signing refuses a transaction
  with no `chainId` (EIP-155 replay exposure), and typed-data signing is kept
  off the money path entirely, since a signed permit is not a transaction and
  would bypass simulation, deltas, caps and audit. `defi_trade` is classified
  money + high-impact + delegation-blocked, so it is explicit-grant-only and
  the agent cannot self-serve it; it is ANDed with — never a replacement for —
  the existing wallet caps, kill-switch and `owner_queue` approval lane.
- **Declarative LLM provider registry (proposal 024 P0,
  `LLM_PROVIDER_REGISTRY`, default on):** one `ProviderSpec` table
  (`modules/llm/provider_spec.py`) now describes every LLM provider — identity,
  credential shape, transport, base URL, capabilities — and the thirteen
  historical hand-maintained provider lists (profiles, `PROVIDER_CONFIG`,
  schema-generator routing, native-tools list, key→provider detection, fallback
  hierarchy, the OpenAI-compat model map, …) are derivations of it. Users can
  declare NEW providers with zero code in `~/.polyrob/providers.yaml`
  (`LLM_CUSTOM_PROVIDERS`): any OpenAI-compatible endpoint (Ollama, LM Studio,
  vLLM, llama.cpp, LiteLLM, Groq, Together, Fireworks, corporate gateways) or
  Anthropic-compatible endpoint (z.ai GLM Coding Plan, incl. bearer auth) —
  served by two generic spec-parameterized clients. Declared models join the
  model registry (ownership routing through the OpenAI-compat surface and
  `check_provider_model`); the `polyrob model` picker UI joins in the L1.5
  surface wave. Byte-identical
  with the flag off or no user file (pinned by a dual-mode characterization
  suite + a provider-list ratchet test); `providers.yaml` is treated as a
  credential-equivalent file (denied to agent file tools — it redirects the
  agent's inference endpoint), and `auth.json` stores are name-denied
  everywhere in preparation for 024 L1.
- **LLM credential layer (proposal 024 L1, `LLM_AUTH_STORE_ENABLED`, default
  off):** `core/llm_auth/` — a durable credential store at
  `~/.polyrob/auth.json` (0600 `O_EXCL` create, cross-process `flock`, atomic
  replace), the single `resolve_credential` oracle (env key → OAuth entry →
  consent-tagged borrowed entry → no-credential sentinel), credential
  **health** (`ok`/`rate_limited`/`exhausted`, `last_status_at`-reconciled so
  a lost exhaustion marker can never resurrect a spent subscription), and an
  auth error taxonomy (`relogin_required` vs throttle). Fail-closed tenancy:
  the store only serves on a single-owner `POLYROB_LOCAL` deployment. The
  credential layer is agent-unreachable by construction (pinned by tests), and
  both secret scrubbers now redact JWT-shaped OAuth tokens via one shared
  pattern. OAuth connect flows (L2) and surface un-blinding (L1.5) are the
  next phases.

### Changed

- **Provider and credential UX is honest end-to-end.** A 15-finding evaluation
  of provider setup and usage flows closed two outright blockers and thirteen
  smaller lies. `polyrob serve` used to import a repo-root module that is in no
  wheel, so *every installed* `polyrob serve` died with a `ModuleNotFoundError`
  instead of the intended no-key refusal — the server entry point now lives in
  the package and gates before importing it. Keyless-by-design providers (a
  local Ollama, the documented rail) were excluded from the gating oracles, so
  a keyless box was refused even when explicitly asked for it; they are now
  first-class. Beyond that: `polyrob doctor` and `polyrob model` derive status
  from one vocabulary (`present` / `malformed` / `missing` / `no key needed`)
  instead of contradicting themselves between a table and its footer; a
  rejected `providers.yaml` row is queryable state in `doctor` rather than a
  transient log warning; `polyrob init` no longer prompts for the API key of a
  keyless provider; `polyrob run -p <unknown>` prints the known-provider list
  instead of a traceback; `GET /v1/models` derives from the spec registry so
  declared providers are discoverable by OpenAI SDK clients; error messages
  name the *session's actual* provider (a `-p zai-coding` failure no longer
  reports "provider openrouter failed" or "from AnthropicCompatClient"); and
  remedies point at `polyrob doctor` / `init` / `config set` / `providers.yaml`
  rather than a repo-relative env path that does not exist on an installed box.
  `POLYROB_<PROVIDER>_MODEL` is now a registered catalog row, and the
  credential-surface write refusal moved into the config oracle so any remote
  surface inherits it.
- **Repo-wide duplication and dead-code sweep.** A verified audit removed
  several thousand lines of unreachable surface — a retired second billing path
  that re-implemented the markup math, an unused A2A client, dead LLM client
  and manager methods (including a third "which provider owns this model"
  implementation), dead database/memory modules and ~20 zero-caller methods,
  orphaned telemetry models, and a 346-line duplicate Markdown formatter — and
  consolidated the survivors onto single sources of truth: one bool/float env
  parser battery, one secret-scrub sequence, one x402 database/telemetry
  helper, one payments network table, one persistent-backend cache, one
  surface-command envelope behind the seven `polyrob <surface>` commands, one
  sidecar-DB path resolver, and one FTS query builder. Behaviour-preserving
  except where the duplication was itself the bug (see Fixed/Security).

### Fixed

- **Credit death is fail-fast again.** The billing block's deliberate
  `InsufficientCreditsError` was being absorbed by the generic exception
  boundaries guarding the native-tools → structured-output → plain-call →
  manual-parse chain, so each absorbed raise bought *another* billable provider
  call: one out-of-credits step could make up to four paid calls before the
  credit sentinel saw it. A timeout at the outer boundary was likewise answered
  by starting another full-timeout call.
- **A stray "billing" or "402" in an application exception no longer halts the
  session.** Both branches that stop the agent classified any exception by bare
  substring match on its message, so a database error on the shipped
  `billing_failures` table, or a 500 on a path containing `/402`, killed the
  run with "PERMANENT ERROR — check API configuration" and pointed the operator
  at their API keys for a bug in application code. Both branches are now
  type-gated to the LLM error family, matching the sentinel that already was.
- **Provider fallback now updates every model source of truth.** After an
  automatic failover the session still named the *failed* provider for billing
  (corrupting per-provider spend and provider-health data), context compaction
  still ran against the dead client, and token budgets were never re-derived.
  Fallback now routes through the same adoption path as the deliberate
  hot-swap.
- **Tool results could be cross-wired to the wrong call.** When identity
  pairing was unavailable, the positional fallback walked calls and results
  with the same index — but a call dropped by pre-execution validation stays in
  the call list and contributes no result, shifting every later call by one. A
  tool message could be built from a *different* call's output while the call
  that actually ran was reported to the model as "not executed", with no error
  surfaced anywhere.
- **Per-session reflection state no longer leaks across tenants.** The memory
  manager is a container singleton, but the reflection model and its billing
  identity were assigned straight onto it — last-writer-wins, so one tenant's
  reflection ran on another tenant's aux model and the usage record was written
  against the wrong user, session and agent.
- **The tool-free-response counter is actually consecutive.** It was never
  reset on a productive step, so it counted empty responses over the whole life
  of the agent: a model that emitted one tool-free step at step 3 and another
  at step 44 drew a "2 consecutive steps" escalation and was scolded for
  something it had not done, and `ALLOWED_REASONING_TURNS` degenerated from
  "one planning turn per run" into "one per two empty responses ever".
- **Two background paths no longer block the shared event loop.** The skill
  curator's tick and the every-10-steps memory save both did synchronous SQLite
  and filesystem work inline on the loop that also serves live chat, API, goal
  and cron sessions — under write contention one curator tick could stall every
  session in the process for seconds. Both now run off-thread.
- **`GET /api/admin/users/search` works again.** It was registered after
  `/users/{user_id}`, so FastAPI matched the path-param route first and every
  search landed in `get_user("search")`. Route order is now pinned by a test.
- Judge-model calls whose structured output failed to bind went **unbilled**
  even though the provider ran and charged for them; the `<environment>` block's
  "Tools loaded this session" line had never rendered (it read an attribute the
  agent does not have); a browser-state exception left a `None` that downstream
  code dereferenced unconditionally; feed mirroring of memory reads/writes had
  raised a `TypeError` on every call since it shipped (swallowed at debug
  level); and a lingering inline copy of the streaming fallback on the billing
  path was converted to the shared helper.

### Security

- **Four policy gates that matched nothing are live again.** Container-tool
  actions register namespaced as `{tool_id}_{action}`, but four policy sets
  listed the *bare* verb — and an exact-match gate keyed on a name the runtime
  never emits fires never, while every unit test passed because the tests
  asserted the same fiction. Consequences: `PAYMENT_APPROVAL_MODE` had **never
  gated invoice creation** under either mode; the scoped tainted-reply
  exemption was dead for email; and the redundant name entries protecting
  high-impact verbs (code execution, deploy/undeploy, the x402 ledger reads)
  were absent. All four sets now carry runtime names, and an action-name parity
  ratchet fails the build if a gate ever again names a verb the runtime cannot
  emit.
- **Correspondent taint is cleared on delivery, not on submit.** The clear ran
  at the top of message submission, before the checks that decide whether the
  message is even accepted — so three rejection paths (unknown agent id, and
  either queue full) re-opened every high-impact tool while the session still
  held untrusted third-party data and no owner turn had entered. Anyone able to
  make the queue reject could open the gate without the owner doing anything.
  The clear now happens at the drain point, where the message provably enters
  the turn.
- **One ordered secret-scrub battery.** The logging filter's private copy of
  the substitution sequence had dropped the JWT rung, so OAuth access and
  refresh tokens survived into log lines — and the fast-path gate skipped a
  bare JWT entirely. The persisted-content scrubber, the logging filter and the
  CLI display scrubber now all delegate to one sequence.
- **An RPC provider error is UNKNOWN, never a confirmed zero.** The JSON-RPC
  helper returned `result` unconditionally, so an `{"error": …}` body mapped to
  `0`. A rate-limited endpoint therefore reported a *confirmed* `$0.00` wallet
  balance; worse, the x402 treasury scan swallowed the failure and returned
  "nothing there", advancing its checkpoint past an unread block range — and
  because the checkpoint refuses to regress, a payer's real transfer in that
  range was lost permanently, without even an unmatched-payment notice. The
  scan now distinguishes "not scanned" from "scanned, empty" and holds its
  checkpoint so the next tick retries. Operators can pin a real endpoint per
  chain with `DEFI_EVM_RPC_<CHAIN>`.
- **MCP HTTP health checks are SSRF-validated.** The connectivity probe built a
  raw session against a user-supplied URL with no validation and redirect
  following; it now uses the same validate-and-pin path as every other MCP HTTP
  hop, with redirects disabled.
- **Workspace confinement no longer accepts sibling directories.** The path
  validator's `realpath().startswith(root)` check passed `/tmp/ws-evil` for a
  root of `/tmp/ws`; it now delegates to the shared containment helper.
- Two browser lifecycle methods and a context manager pair were each defined
  twice in one class body (Python keeps the last definition, so the first pair
  was silently dead), a duplicate money gate in the usage tracker was deleted,
  and `supports_native_tools` now answers from the provider spec first — the
  hardcoded substring list had been shadowing it and contradicting the spec.

## [0.9.0] — 2026-07-25

Reliability, autonomy, and honesty hardening across the agent loop, plus two new
interoperability surfaces (an inbound MCP server and dependency-ordered goals).

### Added

- **Per-session spend budget (`RUN_BUDGET_USD`, default `0` = off):** set a dollar
  ceiling and a run halts honestly the moment its summed real provider cost reaches
  the cap — reported as a stopped run with a budget marker, never a fabricated
  "completed". The cap counts real provider cost (not the marked-up user price);
  sub-agents ride the parent's budget. Surfaced in the agent's environment block so
  the model can pace itself, and delivered honestly over chat and Telegram.
- **Inbound MCP server surface (`MCP_SERVE_ENABLED`, default off):** polyrob can now
  act as an MCP *server*, so an MCP client (Claude Desktop, Cursor) can connect to it
  as a tool provider over `POST /mcp` (JSON-RPC-over-POST). v1 is read-only and
  exposes five tenant-scoped tools — `rob_usage_summary`, `rob_goals_list`,
  `rob_goal_show`, `rob_conversations`, `rob_pending_approvals` — authenticated with
  the same `X-API-KEY` / bearer-JWT / x402 policy as the A2A surface. This is the
  inbound counterpart to the existing outbound MCP *client* (`MCP_ENABLED`).
- **Query-based tool discovery (`tool_search` / `tool_describe`):** the agent can now
  search every tool the deployment knows about by keyword — built-in tools *and* the
  tools behind connected MCP servers (dozens-to-hundreds, previously reachable only
  through the single `mcp` tool) — and get full detail (parameters, capability
  dimensions, honest load/gate status, and how to invoke) for any one of them. Both
  actions are read-only, deterministic (no LLM/embeddings), and reuse the same honest
  status the `<tool-catalog>` renders — money tools are searchable but never shown as
  self-serve-loadable, and delegated sub-agents see the same structured refusals. Rides
  the existing progressive-disclosure gate (`TOOL_PROGRESSIVE_DISCLOSURE`, on under
  `POLYROB_LOCAL`).
- **Dependency-ordered goals:** the durable goal board now supports dependency edges.
  The agent's `goal_create` tool accepts `depends_on: [<goal_id>, …]`, and a goal
  with unmet prerequisites waits until they complete before it becomes eligible to
  run. Block reasons are now typed (`provider_outage` / `needs_input` / `dep_failed`),
  and a goal blocked by a transient provider outage self-heals after
  `GOAL_BLOCKED_PROVIDER_RETRY_MIN` (default 30 min) instead of aging out like a
  stuck goal.
- **SSH code-execution backend (`CODE_EXEC_BACKEND=ssh`):** run `run_code` on a remote
  host over your system `ssh` (`CODE_EXEC_SSH_HOST` / `_USER` / `_PORT` / `_KEY`). It
  is honestly reported as **not** a sandbox by default — a generic remote host runs
  agent code with the SSH user's full privileges — so a server refuses it unless you
  attest the host is hardened/disposable with `CODE_EXEC_SSH_SANDBOXED=true`.
- **OAuth for outbound MCP connections (`MCP_OAUTH_ENABLED`, default off):** SSE/HTTP
  MCP servers whose config declares an `auth: {provider: generic_oauth2, …}` block get
  an injected `Authorization` header, with token minting/refresh persisted
  (Fernet-encrypted) and a single automatic retry on a 401. Forward-looking
  scaffolding; no shipped server declares `auth` yet.
- **Security & trust-model guide page** ([docs/guide/security-model.md](docs/guide/security-model.md)):
  one honest, consolidated answer to "what actually stops the agent from doing
  something bad?" — which gates are in-process heuristics vs. the OS/container
  boundary, where code runs unsandboxed today, and recommendations by deployment
  shape.
- **`AUTONOMY_ENABLED` master switch:** one owner-legible flag that turns the
  self-directed autonomy loops on. Default off for a new local install; on
  automatically under `AUTONOMY_MODE=autonomous` or `AUTONOMY_POSTURE`
  owner-visible/full. First run prints a one-time posture notice (autonomy on/off +
  where data and config live), and `polyrob doctor` gains an `autonomy:` line
  alongside the data dir and active config file.

### Changed

- **Autonomy is now OFF by default for new local installs (default change):** the local
  CLI profile still enables the *interactive* tools (coding, git, knowledge base, memory,
  project-context); the self-directed loops (self-wake, goal board + planner, curator,
  background-review, episodic continuity, self-editing) now require `AUTONOMY_ENABLED=true`
  (or `AUTONOMY_MODE=autonomous` / an `AUTONOMY_POSTURE`). A first run no longer silently
  starts a background agent that schedules goals and rewrites its own skills — it prints
  the active posture and where data/config live instead. Multi-tenant server behavior is
  unchanged. Check state anytime with `polyrob doctor` or `/autonomy`.
- **Dead-target delivery hygiene, now on by default (`DEAD_TARGET_REGISTRY`):** the
  agent stops burning outbound sends on provably-dead targets (a chat you've been
  blocked from, or a deleted conversation) and automatically revives the target the
  next time it hears from it. Only definitively-classified failures are suppressed;
  ambiguous errors are unaffected.
- **Anti-injection framing on context compaction, now on by default
  (`COMPACTION_PROMPT_GUARD`):** the summarizer prompt and the prior-summary block it
  rebuilds are framed so adversarial text captured in a long conversation can't hijack
  the compaction step.
- **Reason-specific outage notices:** the owner-facing "all providers are down" notice
  (rides `LLM_OUTAGE_NOTICE`) now distinguishes the cause — out of credits vs. an auth
  failure vs. every provider exhausted — instead of one generic message.
- **Coding tool tolerates near-miss edits:** `str_replace` now falls back through a
  small ladder of whitespace-tolerant matches (blank-line edges, interior spacing) when
  an exact match fails, and reports which rung matched so the edit stays auditable.
- **Cross-session search paging and ranking:** `session_search` supports
  `before_id` pagination under newest-first sort, and de-prioritizes automation
  (goal/cron) sessions in results so human conversations rank first.
- **Background delegations survive a restart:** a detached (`background=true`)
  delegation that completed while the process was down is now delivered back into its
  session after restart, rather than being silently lost.

### Fixed

- **Chain-aware LLM error classification:** a single structured error taxonomy
  (`core/error_classifier.py`) now drives the loop's fatal-vs-retry and
  billing-vs-transient decisions by walking the full exception chain, so a billing
  failure wrapped inside another error is no longer misread as a generic failure.
- **Goal board correctness under contention:** atomic claim/completion is
  compare-and-swap guarded against double-processing, dependency cycles are rejected at
  creation, and a raced dependency edge is repaired on the next tick.
- **Telemetry write no longer errors on first use:** the per-session LLM-usage log
  handles a missing directory on its very first write instead of failing.
- **Per-session turn serialization:** inbound messages that resolve to the same session
  through different address aliases are now serialized on the resolved session id, so
  two near-simultaneous arrivals can't race.
- **Invoice listing paginates correctly:** a status-filtered invoice list now applies the
  filter in SQL *before* the row limit, so filtered results beyond the first page are no
  longer dropped.

### Security

- **Log redaction gaps closed:** the secret-scrubbing filter is now attached to the two
  log surfaces that were bypassing it, and a hole where secrets in a marker-less shape
  (e.g. a bare token value) slipped past the marker gate is fixed. Real log calls are
  now covered.
- **No raw exception text echoed to callers:** the A2A JSON-RPC internal-error response
  and the inbound MCP-server error paths no longer echo raw exception strings (which can
  carry internal detail) — they return a generic message and log the detail server-side.
- **Credit-death detection is precise:** the sentinel that recognizes an unrecoverable
  "out of funds" `402` now matches the code on a word boundary, so an unrelated number
  that merely contains `402` no longer trips it.
- **Inbound MCP server fails closed on an unresolved caller:** a `tools/call` with no
  resolvable principal is refused rather than served.
- **Coding tool's type-checker no longer inherits secrets:** the LSP diagnostics
  subprocess (pyright/tsc) now runs with a scrubbed environment allowlist, so provider
  keys and other secrets in the process environment are never exposed to it.
- **Correspondent-tainted sessions can't read the financial ledger:** the read-only
  `accounting` / `x402_invoices` verbs are now name-gated with the money verbs, so a
  session tainted by a third-party correspondent can no longer read treasury balances,
  income, or invoice history.

## [0.8.1] — 2026-07-21

### 2026-07-20 — Reliability & honesty fixes (live battle-test hardening)

- **Financial-language honesty**: an unpaid fetch / x402 attempt now explicitly
  states it did NOT pay (the proximate cause of a fabricated "payment sent" claim),
  and the agent is steered to `x402_quote` instead of a rejected `max_amount_usd=0`.
- **Owner-delivery priority lanes**: the user-delivery rail is now priority-ordered
  so a credit-death / halt notice can no longer be starved behind ordinary chatter
  under the flat FIFO send cap.
- **Credit-death sentinel reachability**: the fatal-halt branch the sentinel needs
  was unreachable — it now walks the exception chain to recover the 402's billing
  text, and re-trip notices no longer self-dedupe (each trip carries its timestamp).
- **Message tool**: results acknowledge attached media (killing a retry-to-`BLOCKED`
  loop), carry real content evidence + the true error for the completion judge, and
  resolve `owner` as a target alias before access-tier resolution.
- **Code execution (docker)**: the sandbox workspace bind-mount is writable by the
  forced uid, and `chmod` recurses into pre-existing subdirs.
- **Filesystem tool**: `write_file`/`append_file` coerce dict/list content to JSON
  instead of erroring.
- **Surfaces / Twitter**: a goal's owner-notify message is no longer literally
  addressed to the bot's own handle; Twitter `media_paths` resolve against the real
  session workspace; and the agent is steered away from posting debug-scratch text
  to the live account.

### 2026-07-20 — Dynamic tool rig S3+S4: mcp un-exclusion + create-time narrowing removed

- **`mcp` registers in the CLI/headless container** (S3 tail; browser was un-excluded
  by the maint loop @14381f62): `MCPTool.__init__` is config parsing only, so it left
  `_CLI_INCOMPATIBLE`; registration is gated by explicit `MCP_ENABLED`, the
  autonomous-mode capability default, or local server files (`_cli_extra_gate`).
  Missing gateway secrets (`MCP_GATEWAY_TOKEN`/`ANYSITE_JWT`) now fail loudly at
  load/connect time — an owner ask — never a silent "not found in container".
- **`goal_create` stops narrowing** (S4): under `TOOL_PROGRESSIVE_DISCLOSURE`, an
  inference-only goal no longer writes keyword-guessed `payload.tools` (which
  short-circuited dispatch's wide `default_goal_tools()`); dispatch-time inference
  remains as a widening hint, explicit tools + baseline union unchanged, flag off =
  byte-identical. Seeding doctrine ("omit payload.tools unless deliberately
  narrowing or granting a money verb") stamped into `scripts/seed_goal.py`.

### 2026-07-19 — Dynamic tool rig S1+S2: honest `<tool-catalog>` + self-serve `load_tool` (progressive tool disclosure)

- **Every session can now SEE the whole tool universe and self-serve what it needs**
  (owner directive: the static, silently-narrowed toolset was the rigidity behind
  the "17 steps researching around a missing browser" failure). Gated
  `TOOL_PROGRESSIVE_DISCLOSURE` (default OFF; **ON under `POLYROB_LOCAL`**).
- **S1 `<tool-catalog>` foundation block** (`tools/tool_disclosure.py`, pinned as a
  `TOOL_CATALOG`-origin control message like skills): one line per known tool with
  an HONEST status — `loaded`, `loadable — load_tool("<id>")`, or `gated:<reason>`
  with the remedy channel (`money` explicit-grant-only / `leaf-blocked` /
  `unavailable-on-this-deploy` naming the missing config). Pure render over the
  existing SSOTs (`tools/descriptors.py` + `core/tool_capabilities.py` + the
  container); the system prompt stays byte-stable/cacheable.
- **S2 `load_tool(tool_id)` action**: materializes a `loadable` tool mid-session
  through the SAME `load_tools_from_container` path session creation uses — its
  schemas appear on the next step (registry cache self-busts). Money tools are
  NEVER loadable (explicit owner/goal grant only); delegate-blocked ids refused
  for leaf/sub-agent turns (honors the `DELEGATE_BLOCKED_TOOLS` env override);
  correspondent-taint/posture/approval gates unchanged — loading registers
  schemas, it grants no execution rights. Refusals are STRUCTURED
  (`gated:<reason>` + remedy), killing the silent-drop failure mode.
- S3 (lazy construction for `_CLI_INCOMPATIBLE` heavy tools, browser first) and
  S4 (`goal_create` stops writing narrow inferred `payload.tools`) shipped
  alongside — see the S3+S4 entry above.

### 2026-07-19 — Deliverable reachability: completions attach their files, console deep links, /kb + /files (proposal 021)

- **Goal/cron completion pushes now carry their deliverables.** The owner push is
  built from the run's artifact registry (`agents/task/goals/deliverables.py`):
  files attach to the Telegram message as documents/photos (screened + capped),
  everything else is listed honestly as `server-only: <path> (<reason>)` — never
  again a bare filename the owner can't open. Attaching is gated
  `DELIVERABLES_ATTACH_ENABLED` (ON under `POLYROB_LOCAL`), capped by
  `DELIVERABLES_ATTACH_MAX_MB` (10) / `DELIVERABLES_ATTACH_MAX_FILES` (3).
- **One shared attach-eligibility seam** (`core/surfaces/attachments.py`):
  workspace confinement (relocated from the `message` tool), per-file size cap,
  secret-shaped-filename refusal, bounded prompt-injection threat scan
  (fail-closed on scanner error). The `message` tool's `media_paths` now rides
  the same screen; the delivery rail (`deliver_user_message`) and the
  out-of-band `TelegramBotSink` gained media transport (per-entry fail-open —
  a media fault never takes the text down).
- **Console deep links** (`WEBVIEW_PUBLIC_URL`): completions append the
  owner-auth webview `/session/<id>` link; the daily digest appends a console
  line. Unset ⇒ byte-identical.
- **Webview browses the agent's ACTUAL workspace root:** with
  `POLYROB_PROJECT_DIR` set the agent runs pm() in project-root mode but the
  webview's own process didn't — its file browser showed the EMPTY per-session
  dir while artifacts sat in the project dir. Startup now applies the same
  mode, single-tenant postures only.
- **Telegram `/kb <query>` and `/files [n]` owner verbs** — the phone-first
  owner's read path into the knowledge base and the artifact registry
  (previously CLI-only; "ingested into KB" was write-only theatre from chat).
- **Goal-run prompt teaches attachment:** report a produced file to the owner
  WITH `message(media_paths=[...])`; oversized/refused files get the full
  server path instead.
- **Same-day review fix wave** (two independent review passes):
  layering-ratchet repair (threat scanner dependency-injected out of core),
  content-level secret refusal via `core/secret_scrub` (text AND binary heads),
  write-attribution widened to all ledger output kinds + unattributed files
  listed (never dropped), attached lines carry the absolute server path (text-only
  re-deliveries stay reachable) + `attachments` attrs on `user_delivery` events,
  `MESSAGE_MEDIA_MAX_MB` (45) decouples the explicit `message`-tool cap from the
  10 MB auto-attach cap, cross-tenant media guard in `_notify_owner_done`,
  webview file endpoints refuse credential-shaped files, sink caption truncation,
  `/files` episode window scaling.

### 2026-07-19 — Avatar pipeline: one-time random setup, native headless renderer, voice surfaced

- **Avatar setup is now a ONE-TIME flow: draft → randomize → keep.**
  `pfp generate` mints a RANDOM DRAFT identity (fresh shuffle variant → new face +
  voice per instance) instead of silently freezing the committed stock face every
  install; `pfp randomize [face|voice]` re-rolls the draft (everything / face-only /
  voice-only, studio shuffle semantics); `pfp keep` (or `pfp pick`'s save) accepts it
  and locks the identity PERMANENTLY. A kept identity cannot be changed by any verb —
  `modules/pfp/store.py` raises `PfpLockedError` on any identity-changing write
  (pixels-only re-render of the same identity stays allowed); `pfp push` requires a
  kept identity; a pre-lock-era `pfp.json` is treated as kept. REPL: `/pfp generate` /
  `/pfp randomize [face|voice]` / `/pfp keep`. `--stock` reproduces the committed
  identity, `--seed`/`--variant` pin a specific roll, `--config` keeps the
  frozen-blob path.
- **Native headless renderer (`modules/pfp/still.py`):** the Mindprint dot pass
  ported to Pillow/numpy over the parity-tested field port. Render chain is now
  Chromium (exact engine) → native mesh renderer (same face, no browser) →
  committed reference (STOCK identity only). A randomized identity can no longer
  be silently replaced by the reference PNG's pixels.
- **Setup lets you SEE and HEAR the identity on both surfaces.** CLI/REPL: every
  setup step renders the face inline (truecolor TTY) and the new
  `polyrob pfp say [text]` / `/pfp say` speaks the voice signature through the
  native TTS engine (`modules/pfp/voice.py` — macOS `say` with timbre→clear-voice
  mapping + semitone pitch shift, `espeak-ng`/`espeak`, Windows SAPI SSML;
  fail-open with web pointers when no engine exists). Web: the webview /identity
  page now runs the full setup — live face, DRAFT/KEPT state, 🔊 hear-voice
  (browser speechSynthesis, studio timbre mapping), and draft-only
  re-roll/keep controls over `POST /api/pfp/{generate,randomize,keep}`
  (403 read-only; store-enforced lock contract). Config-shuffle helpers moved to
  `modules/pfp/identity.py` (CLI re-exports them unchanged).
- **`pfp pick` freezes into the instance identity home** (renders png + meta that
  the webview /identity page, invoice cards, and `pfp push` actually read) instead
  of writing the repo's `avatar/config/rob.json` (read-only under pip installs;
  changes never propagated without a manual `generate --force`). `--out` exports
  the chosen config JSON.
- **Generate/randomize now report the full identity + next steps** (face traits,
  the voice signature, and view/re-roll/push/console pointers) instead of a bare
  PNG path; `/pfp status` shows traits + voice too.
- **`pfp push --discord` (flag `PFP_PUSH_DISCORD`, default OFF):** sets the Discord
  bot avatar live via `PATCH /users/@me` (`DISCORD_BOT_TOKEN`, hash-idempotent,
  fail-open) — Discord was the one API-capable surface with no avatar push.

## [0.8.0] — 2026-07-19

### 2026-07-18 — Proposal wave 010A/012/015/016/019-cap: outage honesty + delivery-cap starvation + acceptance gap

- **`LLM_OUTAGE_NOTICE` (default ON, 015 #2):** an owner chat turn that dies on
  total LLM-provider exhaustion (the live OpenRouter-402 shape) now gets one
  static, LLM-free ⚠️ notice over the originating surface (30-min per-chat
  cooldown, fail-open, never for goal/cron runs) instead of pure silence.
- **`llm_provider_exhausted` failure marker (015 #3):** dispatcher failure
  classification now distinguishes a provider outage from a genuine
  refusal/no-op in `goals.last_failure_error`; `intel_scorecard.py` surfaces
  it as a dedicated red flag.
- **Honest episode stats on failure (012 #1):** all dispatcher
  failure-classification paths thread the real `RunOutcome`
  steps/spend/artifacts into `finalize_episode` (previously always 0/0,
  corrupting `noop_ratio` and every consumer of `episodes.outcome`).
- **Self-evolution notifier batching + durable capped record (019-cap #1+#2):**
  `maybe_notify_owner_pending` fingerprints the pending set and re-notifies
  only on change (it had burned 29/30 daily proactive-delivery slots,
  starving the daily digest); a `capped` delivery now writes a durable
  `owner_notice` instead of dropping content irrecoverably.
- **`file_contains` acceptance check (016 #1+#2):** the check type the goal
  planner kept inventing now exists (workspace-relative, bounded read,
  all/any modes); planner + `goal_create` prompts state the exact closed
  type set.
- **`EMAIL_AUTONOMY_RUNTIME` (default OFF, 010 A):** the email process no
  longer runs the goal/cron autonomy runtime, eliminating the coin-flip
  claim of telegram-outbound goals by a process that structurally cannot
  send them.
- **`preferences` explain UX:** field-level schema descriptions + a
  self-correcting missing-`key` error (a goal run had burned its retries
  passing `text=` to explain).

### 2026-07-19 — 019 revalidation fix wave (adversarial 3-reviewer pass over P0–P5)

- **Critical (OpenAI batch tools):** the P5 request-builder extraction left a
  stale `formatted_messages` reference in `_generate_with_tools`'s debug log —
  an unconditional `NameError` (f-strings evaluate eagerly) that broke EVERY
  OpenAI native tool call on the default (non-streaming) path. Fixed +
  regression test that drives the real batch method over a fake SDK.
- **Telegram:** an `act_on_inbound` raise (e.g. `create_session` on exhausted
  credits) unwound past all cleanup — leaking the progress tracker in the
  module registry forever, orphaning the `⚙️ Working…` bubble, and giving the
  user silence. The dispatch is now wrapped: tracker closed, bubble deleted,
  error breadcrumb sent.
- **CLI pairing:** a printed `→` start line could be left unclosed when
  `_should_show_tool` flipped mid-flight (synchronous `delegate_task` sets
  `last_step_sub_agent=True` before its own completion). A PAIRED completion
  now always prints its result line.
- **RunActivity:** eviction is now least-recently-UPDATED (was FIFO-by-first-
  insertion — a long-lived busy session could be evicted by 512 newcomers);
  the snapshot fold now runs AFTER the feed write succeeds, honoring the
  documented "never disagrees with the feed" invariant.
- **Token-streaming brain guard hardened:** fenced ```` ```json ```` starts now
  suppress live deltas too, and a TRAILING brain-state block after prose mutes
  the live stream at the `"current_state"` marker (remainder rides the final
  chunk whole, where the downstream brain scrub works). Content reconstruction
  stays exact; new tests for both shapes.
- **Webview /pending:** the auto-refresh no longer dies permanently after the
  first "Show full" click (visibility tracked separately from the fetch cache).
- **Sub-agent mirror:** `subagent_started` now emits inside the try that
  guarantees its paired `finished`, so a cancellation while queued for a slot
  can't strand the parent phase at `delegating`.
- **Deps:** `openai>=1.26.0` (floor for `stream_options`); anthropic floor
  already adequate (`messages.stream` predates it).
- Also fixed a foreign test's process-wide env leak
  (`test_email_autonomy_gate.py` drove the real `_run_email`, whose
  `CORRESPONDENT_ACCESS_ENABLED` setdefault flipped 6 unrelated telegram
  routing tests to DENIED in full-suite runs). Full suite: 8399 passed / 0
  failed.

### 2026-07-18 — Live run-state observability P5 (proposal 019): true token streaming

- **`LLM_TOKEN_STREAMING` (default OFF):** when ON and the provider client
  implements the new `astream_agent_response` (Anthropic + OpenAI),
  `LLMClientAdapter.astream` yields REAL per-token deltas instead of the
  legacy one-blob chunk — the CLI ResponseBox / webview `stream_chunk` /
  Telegram partials fill as the model writes. OFF = byte-identical legacy.
- **Safety of the stream:** deltas run through a per-call
  `StreamingThinkScrubber` (a `<think>` block split across delta boundaries
  never leaks); a completion starting with `{` (brain-state JSON) suppresses
  live deltas entirely so raw JSON never streams to the user (final chunk
  then carries the whole content — exact legacy shape). Tool calls, usage
  metadata, and the per-call provider-response billing id ride the final
  chunk, so token accounting and billing dedup are unchanged.
- **Provider plumbing:** the batch request builders were extracted
  (`_build_tool_api_params` / `_build_tool_request_params`) so streaming
  issues byte-identical requests; Anthropic streams SDK `text_delta` events
  then parses `get_final_message()` with the same block parser; OpenAI uses
  `stream=True` + `stream_options.include_usage` with by-index tool-call
  fragment assembly. A pre-first-chunk failure falls back to single-chunk;
  mid-stream failures propagate (a silent fallback would double the text).
- The agent loop, `stream_output` funnel, and both stream consumers were
  already N-chunk-safe — no changes there. Not yet live-smoke-tested against
  a real provider (no key on the dev box); flag stays OFF until the owner
  flips it.

### 2026-07-18 — Live run-state observability P4 (proposal 019): machine surfaces

- **A2A:** an approval wait now streams as A2A's NATIVE `input-required` task
  state (back to `working` on resolution) with the action name in the status
  message; `tasks/get` responses carry `metadata.current_activity` (the same
  RunActivity snapshot as the session-status API).
- **OpenAI-compat:** `stream: true` stays buffered (P5 is the token-streaming
  upgrade) but the agent turn now runs concurrently with the SSE body,
  emitting spec-legal `: keep-alive` comment frames every ~15s so long turns
  no longer hit client/proxy idle timeouts; a failure after headers surfaces
  as an error chunk + `[DONE]` instead of a dead socket. Documented honestly
  in `docs/guide/api.md`.
- Proposal 019 status → IMPLEMENTED (P0–P4); P5 (true token streaming)
  remains deferred pending separate owner approval.

### 2026-07-18 — Live run-state observability P3 (proposal 019): webview truthfulness

- **Per-session state banner:** a page-level banner (visible on every tab)
  driven by live feed events — `⏸ Awaiting your approval: <action>` with
  inline Approve/Deny (reusing the webgate pending actions; hidden on a
  read-only console, ambiguity falls back to `/pending`), `↻ retrying`,
  `📦 compacting`. Cleared by the matching resolution/progress events.
- **First-class feed cards** for every 019 kind (`tool_started` shows
  "running…" the moment a tool dispatches; approval/retry/compaction/
  sub-agent/delegation render as compact one-liners instead of raw-JSON
  generic cards).
- **`/pending` + `/autonomy` auto-refresh** (5s/10s, visibility-gated;
  pending skips refresh mid-action or while a body is expanded) — a newly
  blocked approval or a goal that starts running shows without a manual
  reload.
- **Session-list activity badge:** `[● tool: navigate]` / `[⏸
  awaiting_approval]` etc. from the in-process RunActivity snapshot
  (honest absence when another process owns the session).

### 2026-07-18 — Live run-state observability P2 (proposal 019): Telegram progress

- **Live progress bubble** (`TELEGRAM_PROGRESS_EDITS`, default ON; per-owner
  pref `progress.telegram`): the static `⚙️ Working…` Telegram bubble becomes a
  feed-driven live status line — `⚙️ step 3 · → navigate · 2 tools · 45s ·
  $0.02` — edited in place at most once per 2.5s. Wait states override
  immediately: `⏸ Waiting for your approval — /pending`, `↻ rate_limit —
  retrying in 8s`, `📦 Compacting context…`; a still-blocked approval gets ONE
  reminder edit after 10 min (never a new message). Built on a new
  surface-agnostic `TurnProgressTracker`
  (`agents/task/telemetry/live_progress.py`) + a multi-subscriber feed-callback
  seam (`ProductTelemetry.add_feed_subscriber` — the CLI's single
  `_on_feed_entry` slot is no longer the only consumer, so the gateway's
  one-process surfaces can't clobber each other).
- **Autonomous run START notice** (`AUTONOMY_START_NOTICE`, ON under
  `AUTONOMY_POSTURE=full`/autonomous, else OFF): `▶ goal started: <title>` /
  `▶ cron run started: <task>` pushed via the one owner-delivery rail
  (dedup + caps) at dispatch time — the owner no longer learns of autonomous
  runs only at completion or in the daily digest. Digest and $0 gated ticks
  never notify.
- Deferred within P2: the email finalized-turn summary footer (needs
  RunOutcome→OutboundMessage plumbing; email stays buffered and unchanged).

### 2026-07-18 — Live run-state observability P1 (proposal 019): full vocabulary + snapshot

- **Vocabulary completed** (same `RUN_EVENTS_ENABLED` gate, fail-open):
  `compaction_started/finished` (emergency prune + LLM compaction),
  `retry_wait` (all five backoff sleeps in the step-error handler, with
  reason/delay/attempt/provider), `subagent_started/finished` (mirrored into
  the PARENT session's feed with goal preview + duration), and
  `delegation_dispatched/completed` (background delegation lifecycle — the
  dispatch-to-terminal invisibility gap). Provider failover events
  (`provider_failure`/`provider_fallback_success`) gained CLI + `/activity`
  renderings (they reached only the webview before).
- **CLI:** sub-agent + delegation + provider-failover lines render in the
  default view; compaction/retry are bar-visible states (`✱ compacting (llm)`,
  `✱ retry (rate_limit) 8s`) with trace lines under `/verbose` — all via the
  EventSpec registry seam.
- **`RunActivity` snapshot:** a per-session phase machine (`idle | thinking |
  tool | awaiting_approval | compacting | retrying | delegating | done`)
  derived at the ONE feed choke point (never at emit sites), exposed as
  `current_activity` (phase/detail/seconds_in_state/step/call_id) on
  `GET /api/task/sessions/{id}`; `null` for unknown/remote sessions.
- **`polyrob session tail <id> --follow`:** the command's docstring finally
  tells the truth — keeps streaming new feed events live (ordered,
  dependency-free seq-file poll), so a second terminal can watch any running
  session, including goal/cron runs.

### 2026-07-18 — Live run-state observability P0 (proposal 019): no more dead air

- **Span/wait feed events** (gated `RUN_EVENTS_ENABLED`, default ON, fail-open):
  `tool_started` fires the moment a tool is DISPATCHED (`multi_act`),
  `llm_started` the moment an LLM call begins, and
  `awaiting_approval`/`approval_resolved` bracket the approval-provider wait —
  so a long tool, LLM latency, or a blocked approval is visible live instead of
  silent. Tool spans join start→completion via a new `call_id` on both events
  (LLM tool-call id, else a synthesized per-batch id).
- **CLI:** the `→ name(args)` line now prints at dispatch time (paired
  completion prints only the `✓/✗` result line; unpaired completions keep the
  legacy two-line form byte-identically). The status bar's tool segment became
  a live current-activity segment with a ticking clock (`→navigate 43s`,
  `✱ thinking 8s`, `⏸ approval: send_email /pending`); a blocked approval also
  prints a full-width notice (never muted by `/quiet`). `polyrob run`'s live
  activity line shows the in-flight tool.
- **Loud degradation:** when TelemetryManager init fails, the orchestrator's
  no-op fallback now (a) swallows ANY capture method (`__getattr__` — no more
  AttributeError for newer captures), (b) pushes a visible error line through
  the CLI feed callback ("live activity unavailable"), and (c) sets
  `telemetry_degraded`. `polyrob doctor` gained a live-activity pipeline check.
- **Webview:** `/activity` summarize() branches for all four kinds (per-session
  view renders them automatically); a no-dark-kinds contract test pins CLI +
  webview + formatter coverage for every run-event kind.

### 2026-07-18 — Config control plane (proposal 018, P0–P5)

- **Honest `/config` panel:** unconfigured keys show the real built-in default
  (flags-catalog + posture-aware) instead of a wall of `None (default)`;
  advisory keys (`style.*`) are labeled; every enforced pref key is
  ratchet-tested to have a real enforcement-site consumer.
- **4 dead pref keys wired:** `goals.notify_on_done`, `autonomy.self_wake`,
  `autonomy.background_review`, `outbound.max_new_recipients_per_day` now
  actually enforce (tighten-only merges preserved).
- **`digest.quiet_hours` enforced:** proactive sends inside the window are
  durably held and released at window-end (5-min autonomy-runtime ticker);
  interactive replies unaffected.
- **`core/config_service.py`:** one describe/explain/search/set control plane
  over prefs + the ~409-flag catalog; provenance chains
  (`git config --show-origin` style); secrets never readable back; writes
  route to the existing stores only.
- **CLI:** `/config explain KEY`, `/config search QUERY`, full argument
  completion (subcommands/keys/enum values), and bare `/config` opens an
  interactive settings picker (reuses the /model ReplPicker; Enter seeds a
  ready-to-send `set` command, bools pre-toggled).
- **Webview:** `GET/PATCH /api/webgate/config*` — search/explain/set for both
  namespaces; env-flag writes gated to local/own_ops owner postures.
- **Agent self-awareness:** `<environment>` block shows the CLAMPED autonomy
  mode + both axes + the loaded-tool list; `agent_status` gains `mode=`; the
  `preferences` action gains read-only `explain`; `self_env` hard-denies
  `core/config_policy/` source.
- **Hardening:** `core/env.float_env`; import-frozen numeric flags no longer
  crash the process on a stray `none` value; raw numeric-parse ratchet (65,
  shrink-only).

### Structural F-2: god-file split — webview read-services + ratchets (2026-07-17)

- **Changed:** the webview console now sources a new session's default tools from
  the `/api/task/capabilities` `default_tools` payload (the
  `agents/task/tool_defaults.py` SSOT) instead of the hardcoded
  `['browser','filesystem']` in `chat.js`; the static list is kept only as a
  last-resort fallback if the endpoint is unreachable, so session creation never
  breaks (R-6).
- **Changed (refactor, no behavior change):** the four pure feed-reading handlers
  `api_agents` (multi-agent roster), `api_services`, `api_task` and `api_skills`
  moved out of `webview/server.py` into shared read-services in the agents tier
  (`agents/task/telemetry/agent_graph.py::build_session_agents`,
  `feed_reads.py::build_session_{services,task,skills}`) so the console, CLI and
  HTTP API can reuse them; the routes are thin wrappers, logic copied verbatim,
  with 20 new characterization tests. `webview/server.py` 4316 → 3758.
- **Added:** `tests/test_file_size_ratchet.py` — a shrink-only line-count ceiling
  for the five F-2 god-files + `policy.py`; new behaviour must go in a new module,
  not grow these, and a split lowers its row.

### Structural F-3: fresh-eyes final sweep (2026-07-17)

- **Removed:** dead code — `SubAgentManager.get_file_lock`/`get_api_limiter` +
  their class dicts (zero callers anywhere); the deprecated
  `SessionOrchestrator.get_workspace_dir()` async shim (zero production callers;
  the sync `workspace_dir` property is the one accessor); two zero-importer
  re-export shims (`agents/task/flag_defaults.py`, `surfaces/email/seed.py`).
- **Fixed:** the PydanticDeprecatedSince20 warning cluster — core/config.py's 7
  inert `Field(env=)` → `alias=` (loading was already by field-name matching;
  byte-identical), 10 v1 `@validator` → `@field_validator`
  (tools/mcp/config.py, api/mcp_models.py; parity verified), `class Config` →
  `ConfigDict` (modules/llm/adapters.py, api/a2a/agent_card.py), deprecated
  `json_encoders` dropped (api/models.py, modules/memory/models.py).
  `BotConfig()` now constructs with zero deprecation warnings.
- **Changed:** the four `_int_env` re-implementations now delegate to the ONE
  parser `core.env.int_env` (verified behaviorally identical first).
- **Adjudicated (no code change):** the H-MEM "~650 dead LOC" claim retired
  (subsystem is production-wired); the P4 async-initialize refactor is
  won't-fix (AGENTS.md updated); the `agents/task/constants.py` mass shim-flip
  is won't-fix (hybrid module, 78 production importers of task-tier symbols);
  the `core/config_policy/policy.py` 9-submodule split is scoped and deferred
  to its own session; prod sidecar relocation VERIFIED live (`db_relocated`
  2026-07-17 14:19Z) — the legacy read-both fallback is removable one release
  later.

### Structural F-1: rate limiters consolidated onto core/rate_limit.py (2026-07-17)

- **Changed:** the six in-process rate-limiter forks (three algorithms) are now
  configured instances of ONE canonical module, `core/rate_limit.py`:
  `SlidingWindowLimiter` (MCP exec — `tools/mcp/rate_limit.py` is a back-compat
  shim; user MCP admin; the public x402 invoice throttle; the webview
  connection/event throttles; `RateLimitManager`'s internals), `TokenBucket`
  (moved from `core/surfaces/rate_bucket.py`, now a re-export shim; the api
  middleware burst gate), and `FixedWindowCounter` (the api middleware's
  minute/hour windows). Decision semantics are pinned by characterization tests
  written FIRST against the legacy implementations (27 tests across the three
  previously untested forks + RateLimitManager) and pass unchanged after the
  consolidation. `surfaces/telegram/rate_limit.py` stays separate by design (a
  RetryAfter penalty tracker, not a request-budget limiter).
- **Fixed:** the webview per-IP connection tracker no longer grows one key per
  client IP forever — it now shares the same bounded-LRU key space
  (`max_keys=5000`) the per-session event limiter already had (E5/WS-4
  precedent; rate-limit semantics for active keys unchanged).
- **Added:** `tests/test_rate_limiter_ratchet.py` — a shrink-only allowlist scan
  that fails on any NEW limiter-shaped definition outside the canonical module.

### Structural remainder R-2: DB locations are honest (2026-07-17)

- **Fixed:** `DB_PATH` is real. It was a decoy — config anchored it, created its
  parent directory, and `polyrob update` snapshots trusted it, but the app always
  opened the hardcoded `<data_dir>/database/bot.db`. The default now matches
  reality (`data/database/bot.db`) and `database_manager` honors an explicit
  `DB_PATH` behind a refuse-to-guess guard: if the real database still sits at
  the derived location and the configured path doesn't exist, startup raises
  with the exact move instructions instead of silently opening a fresh empty DB.
- **Fixed:** `telemetry_events.db` and the opt-in `messages.db` mirror moved to
  the data-home axis (`core.runtime_paths.sidecar_db_path`, read-both/write-new).
  They previously lived under the SESSION artifact tree (`<data_home>/sessions/`
  on prod-shaped installs) while the backup manifest expected `<data_home>/<name>`
  — so `polyrob update` snapshots silently missed the live files. Snapshots also
  capture the legacy files explicitly until relocation.
- **Added:** a one-shot, clobber-proof boot relocation
  (`core/sidecar_relocate.py`) moves an existing legacy file to the data home on
  the first telemetry touch per process, audited via the new `db_relocated`
  telemetry event kind. Fail-open — any error keeps the read-both fallback.

### Structural remainder R-4: core/security promotion + layering inversions (2026-07-17)

- **Added:** `core/security/` — the tier-0 home for the security primitives:
  `secret_guard` (secret/credential path detection), `untrusted_wrap`
  (prompt-injection DATA framing), and `forged_turns` (forged-turn kind
  constants). The old `agents/task/agent/core/` paths remain as re-export
  shims; tools/controller importers now use the core home. Importing these
  modules pulls zero upper-tier code (pinned by tests).
- **Changed:** `core/surfaces/inbound_webhook.py` no longer imports the surface
  tier — `core/surfaces/act.py` owns `InboundResult` + an actor-registration
  seam; `surfaces/telegram/harness.py` registers the shared dispatch at import.
- **Changed:** `modules/x402/middleware.py` no longer imports `api.auth_state` —
  `api/app.py` installs the auth-state writer at mount
  (`install_auth_state_writer`).
- **Added:** a 5-tier import-boundary ratchet (`tests/test_layering_ratchet.py`)
  seeded with the 126 existing upward edges, shrink-only; the core→agents
  allowlist tightened 35→34.

### Structural remainder R-1: one canonical .env precedence (2026-07-17)

- **Added:** `core.paths.env_file_candidates()` — the single source of truth for
  which `.env` files configure the process and in which precedence order.
  `load_env`, `/config check`, the CLI first-run guard, and the `polyrob update`
  snapshot all derive from it now (layering behavior unchanged).
- **Changed:** `polyrob update` snapshots additionally capture the legacy
  `~/.rob/.env` transition fallback (it can still hold live keys via the
  read-only fallback layer), so a rollback restores the whole user env state.

### Money ledger: two statements, never summed (2026-07-16)

The daily digest and the `accounting`/`/status` views used to merge the owner's LLM/API
bill into the agent's own wallet spend before computing one "net" figure — on
2026-07-16 this told the owner "earned $0.00, spent $2.47, net $-2.47" while the
agent's wallet sat untouched at $10 USDC (the $2.47 was 100% API cost, none of it the
agent's own spend). The ledger now shows two statements that are never added together:

- **Treasury** — the agent's own money (USDC): income, spend, pending invoices, and
  `net = income − spend`. Runtime/API cost never enters this figure.
- **Runtime cost** — the owner's money (compute): window + lifetime spend and call
  counts. It has no "net" — there is nothing to net compute cost against.

"Earned" is retired in favor of income/spend, and the old merged fields are gone with
no fallback — a caller still reading the merged figure will error instead of silently
showing a wrong number again. A balance is only ever shown when the provider actually
exposes one; unknown now renders as omitted, never a misleading `$0.00`.

- **Removed:** the autonomy budget gate — `AUTONOMY_BUDGET_USD`,
  `AUTONOMY_BUDGET_WINDOW_DAYS`, `BUDGET_AWARE_AUTONOMY`, and the
  `budget.autonomy_daily_usd` preference/onboarding prompt. It was a $10/day *rate*
  ceiling that can't protect a finite balance — the agent was under budget every single
  day while the provider balance ran to zero — and it gated on the merged figure above,
  so an x402 wallet payment could eat into the agent's compute budget. Every
  wallet-spend cap (`WALLET_DAILY_CAP_USD`, venue caps, payment approval mode,
  correspondent-taint, x402 invoice caps) is untouched. Nothing now throttles burn rate
  on its own — the provider's 402 and the credit sentinel are the backstop.
- **Fixed:** the credit-death sentinel could only trip from a cron or goal run, so an
  interactive chat that hit a real provider 402 — the one place the owner would
  actually notice — never latched it. There is now one universal trip site reached
  from every run path, interactive included.
- **Fixed:** a failed Telegram-bound run used to go silent — the "already delivered
  live" skip assumed the run had succeeded. A failed run now always tells the owner.

### Structural-wave verification & completion pass (2026-07-16)

Cleanup-and-completion over the WS-1..WS-7 wave; zero behavior change except
where marked.

- **refactor(core):** the data-home resolver trio is ONE rule — verified
  `POLYROB_PROJECT_DIR` never changed the data-home value (workspace placement only), so
  `core.bootstrap._resolve_cli_data_home` and `core.runtime_config.get_data_root` now
  delegate to `core.runtime_paths.resolve_data_home` (three-way parity test added).
- **refactor(core):** `core/tool_catalog.py`'s second hand-classification folded into the
  WS-2 capability module — `TOOL_PERMISSIONS` lives beside the capability rows and the
  catalog risk tiers are DERIVED (high = external-write permission, medium = high_impact
  without one), memberships parity-pinned. `VALID_TOOL_IDS` (skill_manager) is now derived
  from the capability table (verified set-equal first); the T12 vocabulary test, made
  tautological by that derivation, now checks gate ids against the independent
  registry-side vocabulary.
- **refactor(config):** WS-1 phases 3–4 landed — the 32 core-adjacent consumer files
  (tools/, cron/, modules/) import from `core.config_policy` directly (only the five
  shim-tail-symbol imports remain on `agents/task/constants`); the `flag_defaults` bridge
  moved to `core/config_policy/flag_defaults.py` (old path re-exports); all 15 core lazy
  config_policy imports promoted to top level with their fail-open/fail-closed guards kept
  on the calls; 12 never-referenced underscore re-exports + a dead `import logging`
  trimmed from the shim; stale pre-wave comments corrected across core/tools/modules.
- **fix(paths):** cli/commands' remaining 22 `or "data"` fallbacks (a latent CWD write
  when no container/config is present) now route through `data_dir_or_home()`; the path
  ratchet's single-quote blind spot closed (5 hidden `action_registration.py` sites fixed,
  patterns extended); 13 ratchet baseline rows deleted, `handlers.py` 7→2.
- **docs(flags):** `DELEGATE_BLOCKED_TOOLS` catalog row fixed (11 → 15 ids; derivation +
  live anchors noted); flags catalog + user-guide refs regenerated. AGENTS.md now
  describes the derived gate sets and `core/tool_capabilities.py`.
- **fix(tests):** the 4 order-dependent failures are gone — TWO root causes: (1) tests
  building a CLI container imported the dev box operator's REAL env files
  (`~/.polyrob/.env` keys, legacy `~/.rob/.env` provider pins, `config/.env.production`
  backfill) into `os.environ`; the suite now disables the backfill session-wide and a
  narrow per-test guard restores the operator-var set (provider pins, owner binding, API
  keys, and the frozen-security flags `polyrob init` applies in-process) — deliberately
  NOT a home redirect, which shadowed tests that isolate via `Path.home`.
  (2) `test_ledger_error_fails_closed` asserted outside its broken-ledger patch and
  depended on a fresh-process ledger failure; the assertion moved inside the patch.

### Revalidation fixes — pre-existing main failures (2026-07-16)

Found by the full-suite revalidation pass after the WS wave; each verified pre-existing
at 370843bd before fixing.

- **fix(llm):** `modules/llm/adapters.py` imported the four provider client modules at top
  level, so EVERY entry-point import (each `polyrob` CLI invocation, every uvicorn worker
  boot) eagerly loaded the anthropic + openai + google.generativeai SDKs. Now
  TYPE_CHECKING-only (the classes were annotation-only there); `cli.polyrob` imports zero
  heavy SDKs and `test_import_layering` is green.
- **fix(docs):** recovered 10 lost plan/review docs from git history (referenced by
  committed docs but only ever present in the shared working tree); the three never
  committed anywhere are grandfathered with an audit trail. `test_doc_consistency` green.
- **fix(tests):** the nginx deploy guards read the retired `deploy_unified.sh` tombstone;
  they now encode the same invariants against the live deploy surface
  (`deploy_webview.sh` installs the ownops vhost; no live script installs the demoted
  proxy `nginx.conf`; `/opt/polyrob` anchor).
- **Known-not-fixed (documented):** 4–5 order-dependent test failures (`test_identity`,
  `test_budget_gate`, `test_goal_dispatcher` child-tools, `chat_resolver_parity`,
  `protected_config_guard`) — all pass in isolation; root cause is `load_env` importing
  the dev box's real `~/.polyrob/.env` into `os.environ` mid-suite. Needs a suite-wide
  env-sandbox fixture (own change, own blast radius).

### Structural upgrade WS-2..WS-7 slices (2026-07-16)

Same-day continuation of the WS-1 wave; every item ratchet- or parity-tested.

- **WS-2 (tool capabilities):** ONE per-tool capability table (`core/tool_capabilities.py`;
  orthogonal dimensions `money`/`high_impact`/`delegate_blocked`/`exec`/
  `readable_while_tainted`). `MONEY_TOOLS`, `DELEGATE_BLOCKED_TOOLS` (env override kept) and
  `HIGH_IMPACT_TOOL_IDS` are now derivations, byte-identical memberships parity-pinned;
  `register_optional_tool` refuses an unclassified tool, so a new tool can never silently
  skip every gate. Verb-level sets stay hand-curated at their gates (T12 keeps them in sync).
- **WS-3 (paths):** `core/runtime_paths.py` gains `data_dir_or_home()` /
  `goals_db_path()` / `cron_db_path()`; ~25 sites that fell back to a relative `"data"`
  (a latent CWD/install-tree write) now resolve the data home, incl. `skill_usage`'s
  repo-root anchor, both browser screenshot fallbacks (+ session-id cleaning, also in the
  trace filename), and 9 operator scripts' hardcoded `data/*.db` / `/var/lib/polyrob/*`
  argparse defaults. New ratchet `tests/test_path_ratchet.py` freezes the remaining
  constructions per-file, shrink-only. Deferred with notes: `bot.db`/`messages.db`/
  `telemetry_events.db` location moves (need a data migration) and the `.env`-candidates
  helper. Tenant-dir conventions documented (`core/instance.py::self_tier_root`) — two
  deliberate grammars, one per path axis, not to be unified on disk.
- **WS-4 (rate limiting):** two real leaks fixed — `api.middleware.RateLimiter`'s
  `_cleanup_old_buckets` was never called (per-user dict grew for the process lifetime on a
  network-facing surface; now amortized-swept), and the canonical
  `core/surfaces/rate_bucket.TokenBucket` now prunes fully-refilled idle keys
  (exact-semantics eviction). Full 6-fork consolidation deferred: it changes throttling
  shape (token bucket vs sliding window) and three forks have no characterization tests.
- **WS-5 (layering edges):** `cli/gitignore.py` → `core/gitignore.py` (shim kept), killing
  core→cli; `core/initialization.py`'s dead top-level `agents.personality` imports deleted
  (layering-ratchet allowlist tightened 37→35).
- **WS-7 (SSOT tail):** `api/openai_compat/model_map.py` resolves a bare registered model
  slug via registry membership (grok/glm/kimi no longer misroute to the env default);
  `scripts/seed_goal.py` and the telegram owner-interactive toolset now source from named
  `TOOLSETS` entries (`earn`, `owner_interactive`).

### WS-1 — config-layer relocation: core↔agents.task cycle broken (2026-07-16)

Deep structural wave following T1–T12.

- **refactor(core):** relocated the cross-cutting autonomy/mode/posture/payment-policy cluster +
  `AutonomyConfig` (≈1230 lines) from `agents/task/constants.py` into the new core-tier package
  `core/config_policy/` (`policy.py`). `agents/task/constants.py` re-exports every public and
  externally-referenced private symbol unchanged, so all ~126 importers are byte-compatible; new
  code should import from `core.config_policy`. Added a `reset_autonomy_mode_warnings()` test seam.
- **refactor(core):** flipped all 15 `core/ → agents.task.constants` back-edges to
  `core.config_policy`, so `import core.config_policy` pulls zero `agents.*` modules — the
  `core ↔ agents.task.constants` cycle is one-directional (`agents → core`) at last.
- **test(core):** added `tests/test_layering_ratchet.py` — bans `core/` imports of
  `agents.task.constants` and enforces that the remaining `core→agents.*` edges (WS-1 phases 3–4 +
  WS-5 targets) may only shrink.

### Structural cleanup wave T1–T12 (2026-07-16)

Twelve fixes from the 2026-07-16 four-way structural audit (duplication / path handling /
layering / sources-of-truth).

- **T1 (data-loss fix):** 10 sidecar DBs (`slack/signal/discord/x` dedup, `wa_window`,
  `group_allowlist`, `conversations`, `outbox`, `surface_state`, `deployed_apps`) registered in
  `core/db_manifest.py` — `polyrob update` backup/rollback silently skipped them. Grep-based
  completeness contract test added.
- **T2:** `/capabilities` no longer advertises the deprecated `x-ai/grok-4.1-fast` — default
  model now comes from `llm_client_registry.get_default_model` (env-overridable).
- **T3:** `credit_sentinel`'s fallback path resolution follows `resolve_data_home` (dropped its
  unique `DATA_ROOT` precedence — the spend/halt gate could latch in the wrong tree).
- **T4:** the fail-CLOSED identity-scan write gate is ONE base-class implementation for all
  three identity-doc writers (self/contract/owner) — was copy-pasted ×3 (security-drift hazard).
- **T5:** `VALID_TOOL_IDS` covers all registrable tools (`shell`, `process`, `self_env`,
  `hf_deploy`, `github`, `x402_pay`, `alchemy`, `collabland` were rejected as invalid);
  registry-parity contract test added.
- **T6:** one canonical telegram recipient resolver
  (`user_delivery.resolve_telegram_recipient`); cron delivery delegates and its no-sink case now
  leaves a durable `owner_notice` instead of a silent drop.
- **T7:** deleted dead `modules/database/connection_pool.py` (zero importers, divergent PRAGMAs).
- **T8:** `core/activity_evidence.py` — one ledger/episodes evidence layer shared by the owner
  digest and `polyrob recap` (numbers can no longer diverge).
- **T9:** `core/event_kinds.py` — SSOT for all 33 durable event-log `kind` strings + a producer
  contract test; activity feed / spend rollup / digest consume the constants.
- **T10:** a bare `PathManager()` routes through `resolve_session_data_root` (closes the RC-1
  "two session trees" landmine — `DATA_ROOT`-only default).
- **T11:** runtime logs resolve to `<data_home>/logs` (new `POLYROB_LOG_DIR` override) instead
  of the install tree; packaged/read-only installs can log.
- **T12:** cross-consistency contract tests for the six dangerous-tool gate sets (money ⊆
  delegate-blocked, correspondent-gate coverage incl. namespaced trade verbs, gate ids ⊆ tool
  vocabulary, verb-substring sync).

### AUTONOMY_MODE — single-owner capable-by-default master switch (2026-07-16)

Proposal 013 (owner directive 2026-07-15): the recurring "session has no web_fetch/twitter",
"planner: REAL BLOCKER tool unavailable", and "can't approve emails to addresses we don't know"
stalls were all one disease — the framework is deny-by-default and treats missing-permission as
a hard wall. One master switch, `AUTONOMY_MODE=supervised|autonomous` (never "yolo"/"unleashed"
in code, flags, or docs), makes a genuinely single-owner instance capable-by-default without
touching money-spend, host access, or secrets. `supervised` (default/unset) is byte-identical to
pre-013 behavior; `autonomous` is only effective on a single-owner deployment (`POLYROB_LOCAL` +
a bound owner principal via `POLYROB_OWNER_USER_ID`/`_TELEGRAM_ID`/`_EMAIL`) — otherwise it
clamps back to `supervised` with a one-time WARN, so a multi-tenant server can never drift into
it.

- **Capability-flag groups default ON** under effective autonomous mode
  (`_mode_capability_default`): `TWITTER_ENABLED`, `MCP_ENABLED`, `GROUP_CHAT_ENABLED`,
  `EMAIL_SURFACE_ENABLED`, `X402_INVOICE_ENABLED` (receive-side only),
  `MESSAGE_AUTONOMOUS_ALLOWLISTED`, `CORRESPONDENT_ACCESS_ENABLED`,
  `CORRESPONDENT_REPLY_ENABLED` — wired at every consumer seam (`core/config.py`'s MCP gate,
  `modules/eip8004/registration.py`, `core/surfaces/access.py`'s group gate,
  `modules/x402/invoicing.py` + `core/autonomy_runtime.py` for the invoice tool/settlement
  watcher pair), plus `CORRESPONDENT_REQUIRE_APPROVAL` inverted (defaults OFF under autonomous).
  An explicit per-flag env always wins.
- **Autonomous toolset** — `AUTONOMOUS_MODE_TOOLS` (never money-spend/host) is granted to a bare
  session, the goal dispatcher's default toolset, the planner's session-tools (`+web_fetch`), and
  the Telegram interactive toolset, all gated on `full_autonomy_enabled()`; `VALID_TOOL_IDS`
  gained the vocabulary needed to express the grant.
- **Two-lane approvals** — a new `auto_notify` provider (allow + `tool_auto_approved` audit event
  + post-hoc owner notification — "act-and-report") becomes the default under autonomous mode for
  an unset/`auto`/`interactive_cli` `APPROVAL_PROVIDER`. A fixed always-owner-queued lane
  (`_ALWAYS_GATED_VERBS`: the four `self_env_*` verbs, `mcp_install`, plus the aspirational
  `self_modify`/`tool_manage`) never moves to `auto_notify` regardless of mode. `hf_deploy`'s
  first-publish maps `auto_notify → owner_queue` (a public HF Space is not something to
  act-and-report after the fact).
- **Outbound policy ladder** — `OUTBOUND_POLICY` (`open|domains|allowlist|off`, default
  `allowlist`, `open` under autonomous mode) replaces the per-address ACL with a policy+cap model
  (`resolve_outbound_policy`, fail-closed), enforced at the send gates (cap → seed → send →
  record → report), `OUTBOUND_DAILY_SEND_CAP` (default 30) as the first live reader of
  `outbound_count_surface_since`, and a first-contact report that fires only for open-tier sends.
  **Deviation from the original plan:** `outbound.domains` merges via a new `narrow_list` kind
  (the pref can only *intersect* a non-empty operator `OUTBOUND_DOMAINS` env, or define the set
  from scratch when the env is empty) — the plan's specified `union` merge would have let a
  tenant pref *widen* past an operator-set domain allowlist, inverting its polarity;
  `narrow_list` is the corrected, allowlist-safe behavior.
- **Receive-side auto payments, spend stays gated** — `PAYMENT_APPROVAL_MODE` defaults to `auto`
  under autonomous mode, but **only** for `PAYMENT_RECEIVE_APPROVAL_TOOLS = ("x402_request",)`.
  **Deliberate hard line:** the four live-trade spend verbs
  (`hyperliquid_place_limit_order`/`_market_order`, `polymarket_place_limit_order`/
  `_market_order`) keep `owner_queue` pre-approval under **both** modes, including an *explicit*
  `auto` — trading is never act-and-report, closing a gap the initial cut left open (013 T7
  review).
- **Tool-availability transparency** — `TOOL_AVAILABILITY_HINT` (default ON) injects a
  `<tool-availability>` prompt block (`GATED_TOOL_REGISTRY`) disclosing every
  known-but-not-loaded tool with its gate + remedy, so a missing capability is always named
  instead of guessed at or used as an excuse; the goal planner's "TOOL GROUND TRUTH" block reuses
  the same registry.
- **Artifact-existence stamping** — goal/planner prompts now stamp titles/bodies/acceptance
  criteria/past-failure text with `[present, N bytes]`/`[MISSING on disk]` against what's
  actually on the workspace (containment-safe, symlink/traversal-safe boundary lookbehind), plus
  a planner escalate-once instruction after repeated identical blockers.
- **`/config` + visibility** — a Telegram `/config` command (guarded-set → owner-approval
  proposal), `autonomy_mode_display()` surfaced in `/status`, `polyrob owner show`, and
  `polyrob doctor`, matching pending-review parity in the webview, and an autonomy/prefs section
  in `polyrob config show`.

Money-spend, host access (`AGENT_COMPUTE_POSTURE`), and secrets are untouched by this mode in
either direction — see `docs/CONFIGURATION.md`'s `AUTONOMY_MODE` section for the full flag
table. Rollout to prod (T12) is owner-gated and not part of this wave.

### Capability completion — exec everywhere it should be, agent knows where it lives (2026-07-16)

Proposal 014 (from an incident investigation): closes the
session-entry toolset gaps 013 left, makes the posture≥1 dev sandbox Node-capable, and
gives the agent an in-context answer to "where do I live". Everything is gated — a
deployment with nothing set is byte-identical.

- **`default_session_tools()` SSOT** (`agents/task/tool_defaults.py`) — the three drifting
  `['browser','filesystem','task']` literals in `task_agent_lite.py` now route through one
  helper; under effective `AUTONOMY_MODE=autonomous` a bare session gets the ambient
  autonomous grant (never money-spend/compute — those are structurally absent).
- **Telegram interactive toolset is mode- and posture-aware**
  (`surfaces/telegram/interactive_tools.py`) — the owner chat under autonomous mode gets
  the full `AUTONOMOUS_MODE_TOOLS` grant (keeping `goal`/`cronjob`), plus
  `code_execution`/`shell`/`coding` at `AGENT_COMPUTE_POSTURE>=1` via the new
  `with_compute_tools()` SSOT (the goal dispatcher now shares it). `INTERACTIVE_TOOL_IDS`
  still always wins; supervised default unchanged.
- **`CODE_EXEC_DEV_IMAGE`** (default `nikolaik/python-nodejs:python3.11-nodejs20`) — the
  posture≥1 persistent dev container defaults to a python+node image so npm/npx toolchains
  work; the confined ephemeral sandbox keeps `python:3.12-slim`; explicit
  `CODE_EXEC_DOCKER_IMAGE` wins everywhere.
- **`run_code(packages=)` honors the effective sandbox network** — the gate now probes
  `DockerBackend.effective_setup_network()` instead of the raw env, so a dev container
  that auto-bridged (env unset) is no longer wrongly refused pip installs; explicit
  `CODE_EXEC_NETWORK=none` still refuses.
- **Dev-mode exec timeout ceiling is 120s** (was silently 30s) — aligns the backend clamp
  with the shell tool's foreground contract; explicit `CODE_EXEC_MAX_TIMEOUT_SEC` wins;
  confined default stays 30s.
- **`<environment>` foundation block** (`agents/task/agent/core/env_context.py`, flag
  `ENV_CONTEXT_BLOCK` default ON) — instance, platform, data dir, absolute workspace path
  with explicit persistence semantics, posture/mode axes, and a host-executable probe,
  pinned after runtime identity. Emits only under `POLYROB_LOCAL` or effective
  `AUTONOMY_MODE=autonomous`; multi-tenant server sessions unchanged.

### Wallet / crypto security hardening wave (2026-07-15)

A 7-way security + UX review of the wallet/x402/trading stack (2026-07-15)
followed by a same-day fix wave: 2 Critical and all
14 High findings closed, plus most Medium/Low.

- **C1 — pay-side fund-drain closed:** the x402 payment gate now authorizes at the
  tool-call `max_amount_usd` (not the advisory quote) AND re-checks `PolicyGate`
  against the REAL challenge amount before signing; the reserve is held across the
  whole check→pay→record span.
- **C2 — wallet CLI reads the right env:** `polyrob wallet`/`set-cap` load the local
  env before reading the wallet (no more acting on a phantom empty wallet).
- **Owner kill-switch exists (H5/H6):** `polyrob owner halt`/`resume` — a structural
  halt enforced inside `PolicyGate.check`, invoice minting, renewals, and live-trade
  gates; the halt probe fails CLOSED.
- **Turn-origin money gates (H10/H11):** live orders and NAMESPACED crypto trade
  verbs are blocked from forged/autonomous/correspondent-tainted turns; every
  mutating trade verb is origin- and halt-gated.
- **Custody hardening (H1–H3):** the wallet derivation scheme is pinned alongside the
  seed (a legacy wallet can never be silently re-derived), `resolve_scheme` fails
  fast on corrupt meta, wallet policy files are credential-guarded, and the audit
  sink is tamper-evident.
- **Approval integrity (H4):** one approval = exactly one execution; a forged
  approval probe fails closed.
- **Settlement/invoicing hardening (H7–H9 + M-class):** shared-DB and
  settlement-watcher races closed; snapshots now capture the wallet dir and deny
  renamed env copies (M1/M2); unpriceable/non-finite order values fail closed (M10).
- **Owner-facing money truth (H12–H14):** the agent's money self-knowledge corrected,
  `polyrob finance` works standalone, wallet view/export fail friendly on a bad
  seed, and `polyrob doctor` verifies the wallet actually works before reporting
  "on".

### Onboarding finalization — wallet, avatar, identity (2026-07-14)

Closes out the onboarding-finalization wave: the agent wallet is now a one-command,
portable, exportable thing instead of a bare env var, and the avatar/setup surfaces
catch up to it.

- **`polyrob wallet init`** — generates a fresh 24-word BIP-39 mnemonic (shown once) or
  imports one (`--from-mnemonic`) or a legacy raw seed (`--from-seed`); writes
  `AGENT_WALLET_ENABLED`/`AGENT_WALLET_MASTER_SEED` to `~/.polyrob/.env` (chmod 600) and
  offers to point `X402_PAYMENT_RECIPIENT` at the new treasury address so earnings settle
  somewhere spendable. Testnet prints faucet guidance; mainnet prints USDC-on-Base
  guidance.
- **`polyrob wallet export [--venue]`** — TTY-only, typed-`EXPORT` confirmation reveal of
  the mnemonic (bip44) or per-venue `0x`-hex private keys; never agent-callable.
- **Versioned key derivation** — a wallet's scheme (`legacy` PBKDF2 or `bip44` BIP-44,
  `m/44'/60'/0'/0/{treasury,x402,polymarket,hyperliquid}`) is recorded write-once in
  `data/wallet/meta.json` by `wallet init`/import; a pre-existing wallet with no meta file
  is legacy FOREVER — addresses never change. New wallets get `bip44` (mnemonic imports
  cleanly into MetaMask/Rabby). `AGENT_WALLET_DERIVATION` is a recovery-hatch env override
  for a corrupted/missing meta file only.
- **`/pfp` REPL command** (alias `/avatar`; `status|generate [force]|show`) — the avatar
  stays fully optional (nothing auto-generates it); this makes generating/inspecting it
  discoverable without leaving the chat REPL.
- **`polyrob init` bridges from the inline key wizard** — after the first-run key prompt
  saves a usable key, it now offers "Finish full setup now (model, persona, autonomy —
  ~1 min)?" and runs `init --skip-keys` on accept instead of leaving the operator with a
  bare key and nothing else configured; `init` also gained an optional agent-wallet
  opt-in step (default No) and a "Next steps" block (wallet / avatar / surfaces / identity
  / doctor).
- **`polyrob doctor` setup-completeness lines** — wallet/avatar/surfaces/SOUL status,
  gateway-gate-accurate (a flag-on-but-uncredentialed surface reads as configured-but-
  incomplete, not silently "off").
- **`ui.show_avatar` preference** — a per-owner toggle for whether the webview identity
  page renders the avatar.
- **`/model set-default` SSOT fix (G11)** — now keeps `DEFAULT_PROVIDER`/`DEFAULT_MODEL`
  env pins in lockstep with the CLI preference store, instead of drifting apart.
- **`polyrob soul init` (O10)** — scaffolds the operator-authored SOUL identity docs
  (`identity/identity.md` + `identity/operating.md`) so authoring the richer identity
  layer has a discoverable onboarding path instead of requiring hand-authored files.
- **`polyrob-user-guide` skill v2** — adds `references/wallet-and-identity.md` and
  regenerates `references/configuration.md` from the current `docs/CONFIGURATION.md`
  (also absorbs the `AGENT_WALLET_DERIVATION` row and `MESSAGE_AUTONOMOUS_ALLOWLISTED`
  from a concurrent change).
- **Docs** — `docs/CONFIGURATION.md` gains the `AGENT_WALLET_DERIVATION` row;
  `docs/guide/payments.md` gains "Create the wallet in one command",
  "Portability, backup & export" (with the snapshot-contains-seed caveat), and
  "Migrating to a new install" sections; `docs/guide/getting-started.md` documents the
  inline first-run key wizard + its full-setup bridge, fixes the stale ASCII-box
  "Example Session" banner to the real two-line banner, and completes the config-layers
  table (`config/.env.*`, legacy `~/.rob/.env`).

### Update/infra/onboarding hardening — Wave 3 (2026-07-14)

Completes Wave 3 of the 2026-07-14 review (all remaining P2s except the owner-action
secret rotation).

- **`polyrob gateway` launches every surface (H2)** — Discord/Slack/Signal/X now start
  under the gateway when their flags are on (previously silently ignored); an enabled
  surface with missing credentials is WARNED about and skipped. `SurfaceConfig` gains
  the four flag helpers; stale gateway caveats removed from the migration guide.
- **Inline-schema == migration-HEAD contract (U4)** — a new CI test builds a fresh DB
  through the real component creators, stamps at HEAD via the real boot path, and
  requires every shipped migration's `verify()` to pass. It immediately caught a real
  drift: `billing_failures` (v1_3_0) had no inline creator — fresh installs never
  created it and billing-failure records silently failed to insert (now mirrored into
  `AuthTables`). The dead legacy schema initializers in `connection.py` (home of the
  singular `schema_version` table) and the orphan `scripts/migrate_*` one-offs are
  deleted (U11).
- **Doctor env checks (U10/O6)** — Python ≥3.11 floor, `[server]`-extra presence,
  Playwright chromium probe, and a DB-schema-vs-code line (also printed by
  `polyrob update`).
- **`backup_database.sh` (U7)** — now a WAL-safe all-DB snapshot via the update
  engine's Online-Backup path (was: `cp` of a live WAL DB at a path that no longer
  exists), restorable via `polyrob update --rollback`.
- **In-use guard portable (U8)** — the process scan is `/proc` → psutil → `ps`, so
  macOS `--apply`/`--rollback` no longer bypasses the guard silently.
- **Init polish (O2/O3/O4)** — consistent 1/6..6/6 wizard numbering; the closing
  "no usable key" check reads every env layer `polyrob run` honors; DeepSeek's key
  prompt says it can't bootstrap alone.
- **Pairing approvable (O5)** — `polyrob owner pair {pending,approve,revoke}` ships;
  `core/pairing.py` no longer documents a phantom command.

### Update / infra / migration-guide / onboarding fix wave (2026-07-14)

Implements Waves 1–2 (+ selected Wave-3 hardening) of a 2026-07-14 internal
review — the connective
tissue around the good engines: the migration runner, deploy paths, updater safety net,
first-run toolset, and the flagship docs.

- **Migration runner survives self-recording migrations (U1, P0)** — `migrations.migrate
  upgrade` no longer double-records the schema version (`IntegrityError` → exit 1),
  which deterministically rolled back any `polyrob update --apply` containing a
  self-recording migration. The upgrade loop is extracted to a testable
  `apply_pending_migrations()` with recording guarded by `is_version_applied`.
- **`polyrob init` no longer degrades the first run (O1, P0)** — `resolve_toolset
  ("default")` now resolves to the true dynamic default (web_fetch + coding/anysite
  additions), identical to an unset `POLYROB_AGENT_TOOLSET`; accepting the wizard
  default used to silently drop `web_fetch`, breaking the documented first task.
- **Deploy paths migrate the DB and the docs tell the truth (D1/D2/D3, P0/P1)** —
  `deploy_unified.sh` (destructive, dead api+webgate shape) is retired to a hard-exit
  tombstone (legacy body preserved at `deployment/legacy/`); `scripts/deploy_prod.sh` /
  `deploy_from_local.sh` now run `migrations.migrate upgrade` before restart;
  `start_autonomy` schedules `run_boot_migrations` so every posture (telegram/REPL/
  email/gateway) self-heals schema like the API lifespan; `polyrob-email.service` is
  committed and restarted by both deploy scripts. AGENTS.md/DEPLOYMENT.md/
  `deployment/README.md` rewritten around the real headless shape (guard tests keep
  them honest).
- **Updater rollback restores what it promises (U2/U9, P1)** — snapshots carry a
  `scope` (`full`|`db_only`); bare `--rollback` prefers the newest FULL snapshot
  instead of the DB-only pre-migrate one; `--apply` takes ONE snapshot (migrate_guarded
  reuses it); same-second snapshot dirs no longer clobber. Systemd manual steps are
  posture-aware (detect `polyrob*` units + `daemon-reload`; no more phantom
  `polyrob-api`), and the stale "automated apply not wired yet" messaging now
  advertises `--apply` (U3/U6).
- **Migration guide un-staled (H1/H2, P1)** — Discord/Slack/Signal/X marked
  shipped (with honest validation status), compute-posture ladder replaces "not
  supported", learning-loop claim fixed, and every `polyrob gateway` mention carries
  the "doesn't launch Discord/Slack/Signal/X yet" caveat. `tests/test_doc_consistency.py`
  guards the platform claims against re-diverging.
- **Provenance process fix (H4, P1)** — the lost cross-agent-parity design record
  was reconstructed; the 7 still-recoverable referenced
  plan docs are committed; a contract test now requires any internal plan/review doc
  referenced from a committed doc to be committed itself (21 already-lost files
  grandfathered by name).
- **Onboarding docs (O8/O9/O10, P1/P2)** — getting-started.md gains an "Updating"
  section; instances.md corrects the SOUL doc location (flat `identity/*.md`, NOT the
  nested per-user dir — files placed there never loaded) and adds a SOUL authoring
  guide.
- **Hardening (Wave-3 picks)** — trajectory capture runs off-loop
  (`asyncio.to_thread`); bulk datagen export reaches legacy `data/auto/*/sessions/*`
  sessions; `deploy_webview.sh` takes its target from `~/.polyrob/ops.env` instead of
  a hardcoded host; stale ops-script headers corrected (D7/D9/D10/H5/H6).

### Built-in ecommerce / payments finalization (2026-07-14)

The four separate money organs (x402 receive middleware, agent pay-side wallet, agent
invoicing, platform credits) are finalized into one coherent, owner-legible built-in
ecommerce capability: the agent can quote, invoice (text + branded QR image), get paid
(USDC, auto-detected), meter, deliver, and account for itself. Landed as 17 reviewed
tasks; **all new behavior is behind default-OFF flags** (a deployment that enables none
is byte-identical to before). Full reference: `docs/guide/payments.md`.

- **Truth & safety (P0).** Metering now persists on a headless single-owner deploy —
  an owner `user_profiles` row is seeded at startup (`ensure_owner_profile`), closing the
  FK failure that made spend read a false `$0`. The autonomy budget gate and cron ticks
  fail *closed* (a ledger error or `autonomy_halted()` holds dispatch, not runs). Pay-side
  hardening: the wallet PolicyGate runs unconditionally, payment asset is **pinned to the
  canonical USDC** for the configured network (defeats a decimals-spoof cap inflation),
  network binding is fail-closed (V1 names + CAIP-2), `success=false` settlements are
  treated as unpaid, and the kill-switch probe fails closed. Billing correctness: one
  cost entry point (cache-write surcharge preserved), a real `usage_records.request_id`
  column keyed on the provider response id for reachable retry dedup.
- **Invoicing as a product.** Branded QR invoice **cards** (`modules/pfp/cards.py`, pure
  Pillow + `qrcode` + a shipped OFL font; `INVOICE_CARD_ENABLED`, `INVOICE_QR_STYLE`);
  an outbound **media leg** (Telegram photos, email attachments, `message(media_paths)`,
  workspace-confined); free-form **`payer_contact`** ("billed to"); **approval modes**
  (`PAYMENT_APPROVAL_MODE` = `approve` via a durable, remotely-approvable `owner_queue`
  provider with Telegram `tap-` verbs | `auto` within-caps); non-payment **expiry
  escalation**.
- **Facilitator-free settlement.** On-chain USDC **settlement detection**
  (`X402_SETTLE_ONCHAIN_DETECT`): the watcher scans treasury transfers, matches by exact
  atomic amount oldest-first, and settles — with a `transaction_hash` partial-unique
  index + CAS against double-settle, amount-jitter for same-amount disambiguation, and a
  `payment_unmatched` owner notice. A payment-aware cron wake-gate leg.
- **Watchtower subscriptions** (`SUBSCRIPTIONS_ENABLED`, `WATCHTOWER_PRICE_USD` = $10/mo):
  prepaid periods + renewal invoices on the settlement-watcher tick (atomic idempotent
  `apply_settlement`, cron `subscription_lapsed` gate, `polyrob owner sub list/cancel`).
- **Metering→invoice bridge** (`USAGE_INVOICE_BRIDGE_ENABLED`): a tenant-scoped
  `usage_rollup` + `usage_summary` action drafts an invoice from measured cost (never
  auto-sends).
- **ERC-8004 payment-backed reputation** (`EIP8004_PAYMENT_FEEDBACK`): a settled invoice
  offers the payer a `ProofOfPayment`-backed verified-purchase feedback authorization
  (settled + treasury-toAddress + txHash replay guard + agent-id binding; local
  simulation, not on-chain yet).
- **Machine-payer middleware fixes:** exact `(method,path)` route gating (free reads no
  longer paywalled), a shared 402 challenge, `has_other_auth`-gated 503 on a missing
  facilitator, and an **un-spoofable** rate limit on the public invoice endpoints
  (`get_trusted_client_ip` trusted-proxy resolution; `X402_PUBLIC_RATE_*`). Removed the
  dead `x402_access_log` table and the discontinued Google-Charts QR URL.

### CLI candy polish wave (2026-07-14)

A pure visual/UX polish of the `rob` terminal REPL — the current implementation,
made its best self (no renderer changes, no new rendering abstractions):

- Slash commands highlight live while typing (known command / prefix / unknown /
  args each styled distinctly); the completion menu is now actually visible — a
  stock prompt_toolkit palette with per-command descriptions, opening while a
  `/command` is typed and on Tab.
- The hint line under the input is context-aware: mid-turn it shows `^C stop`,
  typing a known `/command` shows that command's usage, idle shows the key hints
  plus one gentle rotating tip (and it stays quiet while the `/model` picker is open).
- Every functional view (`/goals`, `/subagents`, `/todos`, `/pending`,
  `/autonomy`, `/status`, `/usage`, `/tools`, `/toolset`, `/persona`, `/sessions`,
  `/history`, `/session`, `/telemetry`, `/finance`, `/journey`, `/skills`,
  `/cron`, `/config`, `/kb`, `/mcp`, `/learn`, `/self`, `/approve`, `/context`)
  now shares ONE visual grammar: a 2-space gutter, one table style, aligned
  label/value rows, one empty-state phrasing with actionable hints, and one
  status-glyph vocabulary (`●`/`✓`/`✗`/`○`/`⚠` from the theme) — the per-view
  emoji vocab (🟢🟡✅🔴⬜⚪⏱️) is retired.
- Sub-agent steps render on a quiet tree-prefixed lane (`  └ researcher · step 3`).
- Status bar: ctx% turns yellow at 80% and red at 90%; the in-flight verb rotates
  on long turns (`cooking… → thinking… → …`); the previously-dark autonomy line
  (goals/cron counts) is now populated by a slow fail-open background poll.
- New shared modules: `cli/ui/candy.py` (plain-string view grammar helpers),
  `cli/ui/slash_highlight.py`, `cli/ui/hints.py`, `cli/ui/autonomy_poll.py`;
  glyph/style vocabulary consolidated in `cli/ui/theme.py`. No new env flags.

### Owner-UX Phase 4 — surface parity: recap core, Telegram owner verbs, Preferences page (2026-07-14)

Closes out the owner-UX usability wave's Phase 4 (surface parity): the CLI/REPL,
every chat surface (Telegram — and everything sharing its dispatch: WhatsApp,
Discord, Slack, Signal, X, Email), and the web Console now expose the same
read-only situational-awareness verbs and the same typed-preference control
surface, instead of each surface having absorbed a different slice of the
recent agent waves.

- **Surface-neutral recap core** (`core/recap.py`) — the episodes/events/
  skills/ledger assembly behind `/journey` was extracted out of the CLI
  rendering layer into one pure, dependency-injected `build_recap`, so any
  surface reuses the exact same data-gathering instead of re-implementing it.
  Hardened this pass: `_parse_window` now rejects a window that parses to
  something unusable — non-finite (`nan`/`inf`, e.g. `"1e400d"` overflowing
  to `inf`, or `"nand"` parsing as `float("nan")` because the trailing char
  happens to be the `d` suffix) or absurdly large (> ~10 years, e.g. a
  30-digit day count) — with the same friendly `ValueError` as a malformed
  label, rather than relying on `int(nan)` coincidentally raising somewhere
  downstream. Telegram's `/recap [window]` exposes the raw label to chat
  input, so this is a real hardening, not just belt-and-suspenders.
- **Telegram owner verbs** (shared by every surface on the same dispatch):
  `/status` (bound-session state, goal counts, next cron run, cost over the
  trailing 24h), `/recap [window]` (alias `/journey`), `/goals` (board
  summary), `/prefs` (read-only effective preferences) — owner-gated by the
  resolved principal (the local-CLI bypass is never honored on a network
  surface), tenant-scoped, and backed by the SAME primitives `polyrob owner`
  and the REPL slash commands use.
- **Webview Preferences page** (`/preferences` + GET/PATCH
  `/api/webgate/preferences`) is real: schema-driven from
  `core.prefs.PREF_SCHEMA`, safe keys apply on write, guarded keys need an
  explicit `confirm:true` (409 without it), `WEBVIEW_READ_ONLY` blocks all
  writes. A same-wave review caught and fixed a confirm-bypass in the parallel
  T3 commit: a truthy non-boolean `confirm` (the string `"false"`, or `1`) was
  being accepted as confirmation — the PATCH handler now requires a literal
  JSON `true` and 400s on a malformed/non-dict body or a missing `value`.
- **Hardening**: `surfaces/telegram/harness.py` — `_status_reply`/
  `_goals_reply` now share the caller's already-open `GoalBoard` instead of
  each opening a second connection to the same `goals.db`; `/status`'s
  "Cost today" line is relabeled "Cost (24h)" (it's a rolling window, not a
  calendar-day figure).
- **Docs**: `docs/guide/architecture.md` gains a "Chat-surface owner
  commands" table documenting `/status`/`/recap`/`/goals`/`/prefs` (plus the
  pre-existing `/pending`/`/approve`/`/asks`/`/allow` verbs) for every chat
  surface; `console.md`'s Preferences section and `cli.md`'s REPL command
  table were already accurate for this wave.

## [0.7.0] — 2026-07-14

### Model registry refresh — current Anthropic lineup + new OpenRouter Grok/GLM (2026-07-14)

Brought the model registry (`modules/llm/model_registry.py`, the pricing/limits
SSOT) up to the current model landscape; the per-provider clients read from it, so
this is registry-scoped. Provider defaults are unchanged (`anthropic` stays
`claude-sonnet-4-5`, `openrouter` stays `z-ai/glm-5.2`).

- **Anthropic — added the modern lineup:** `claude-fable-5` ($10/$50),
  `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` ($5/$25),
  `claude-sonnet-5`, `claude-sonnet-4-6` ($3/$15) — all 1M-context native, 128K
  output, with dotted/short aliases. These use **adaptive** thinking, so the
  registry deliberately leaves `thinking_budget_tokens` unset (`budget_tokens` is
  rejected with a 400 on Fable 5 / Opus 4.7-4.8 / Sonnet 5) — `get_thinking_config`
  returns `{}`, byte-identical to prior behavior. The 4.5 family + Opus 4.1 stay as
  the active legacy tier.
- **OpenRouter — new Grok:** added `x-ai/grok-4.5` (500K ctx, $2/$6, newest xAI
  flagship) and `x-ai/grok-4.20` (2M ctx, $1.25/$2.50). The unknown-`grok`
  fallback now targets `grok-4.5` instead of the 404'd `grok-4.1-fast`.
- **OpenRouter — GLM tiers + drift fixes:** added `z-ai/glm-5.1`,
  `z-ai/glm-5-turbo`, `z-ai/glm-4.7-flash`; refreshed `z-ai/glm-5.2` to its live
  price/output-cap ($0.93/$3.00, 32K max out; was $1.20/$4.10, 256K) and
  `x-ai/grok-4.3` context to 1M (was 2M) with a cache-read price. All prices
  re-verified against the live OpenRouter models API on 2026-07-14 and pinned in
  `tests/unit/modules/llm/test_openrouter_pricing_verified_2026_07_14.py`.

### CLI rendering finalization — no more corrupted REPL frames (2026-07-13)

Root-caused and fixed the REPL screen corruption (ghost prompt frames, stranded
status rows, floating spinner glyphs, raw `INFO httpx:` lines in the transcript):

- **Logging containment (the root cause):** component-logger creation no longer
  bounces root/handler levels back to INFO; noisy library loggers (httpx et al.)
  are pinned at setup in every process; console and file sinks have independent
  levels (CLI terminal shows ERROR+ only while `bot.log` keeps INFO); the stderr
  handlers resolve `sys.stderr` at emit time so nothing can write past
  prompt_toolkit's `patch_stdout` coordination; httpx records no longer
  double-emit. On the server this newly activates the library-logger pinning
  too — the old `initialize_task_logging` path had been dead code (import of
  a nonexistent name), so prod `bot.log` no longer records per-request httpx
  INFO lines.
- **REPL hardening:** `Ctrl-L` clears + repaints (corruption recovery); the
  SIGINT handler schedules its notice onto the loop instead of painting from
  the signal frame; the per-event usage poll is throttled (0.5s).
- **Hygiene:** dead `cli/ui/pick.py` removed; doc anchors corrected.

### Correspondent conversation architecture fixes (2026-07-13)

Fixes across the correspondent conversation architecture — the
reply→origin-session loop now actually works, time-stretched conversations
survive session death, and multi-contact outreach stops hitting silent walls:

- **Ephemeral delivery rail fixed (default-on bugfixes):** a correspondent
  reply into a resident+completed session now WAKES the run loop (the
  pending-input probe sees ephemerals), unconsumed ephemerals survive
  eviction/restart (persisted in `message_history.json`), and the queue is
  bounded (`MAX_EPHEMERAL_MESSAGES`, default 30, drop-oldest).
- **Reply bindings on every outbound path:** the proactive `message` tool now
  seeds the correspondent registry itself (it previously used a synthetic
  session key no seed could resolve — first-contact replies were DENIED on
  every surface). Seeding runs BEFORE the send; a cap-refusal blocks the send
  instead of orphaning the reply; the cap exempts already-known addresses.
  The seed guardrail moved to `core/surfaces/seed.py` (email module re-exports).
- **Real email threading:** outbound email mints + returns its Message-ID,
  sets `In-Reply-To`/`References`, and each outbound is bound to its sending
  session via registry thread-anchor rows — two sessions emailing one address
  now each get their own replies. Same-tenant address-only ambiguity routes to
  the most recent conversation (`CORRESPONDENT_RESOLVE_LATEST`, default ON);
  cross-tenant stays denied.
- **ConversationStore (`core/surfaces/conversations.py`):** durable
  per-(tenant, surface, address) conversation container — bounded message log,
  context block prepended to every injected reply, `contact_history` action
  (per-address transcript or who-replied listing), and **session re-pointing**:
  a reply to a dead session resumes into a fresh session with full context
  (`CONVERSATION_RESUME_ENABLED`, default ON) instead of being silently
  dropped.
- **Scoped reply-while-tainted** (`CORRESPONDENT_REPLY_ENABLED`, default OFF):
  opt-in exemption letting a tainted session answer EXACTLY the correspondent
  who wrote in (1:1, no cc/bcc, `CORRESPONDENT_REPLY_MAX_ROUNDS`/24h,
  fail-closed) — unblocks autonomous multi-round exchanges when the operator
  chooses.
- **Owner UX + hygiene:** pending correspondents now appear in
  `polyrob owner pending` (+ `correspondent_pending` event on seed);
  `polyrob owner approve --all [<surface>]` bulk-approves;
  `CORRESPONDENT_TTL_DAYS` wires the never-called `purge_expired`; taint is
  lock-guarded and source-tracked; the shared inbound handler serializes
  per-chat (KeyedLock) so rapid cold messages can't double-create sessions.
- Cut per owner decision mid-wave: the planned OutreachStore campaign
  subsystem (the ConversationStore already answers who-was-contacted /
  who-replied; grouping is the agent's job via goals/notes).

### UI surface parity wave — webview control surfaces + CLI verb parity (2026-07-12)

Closes the gaps from the 2026-07-12 UI-surface review (each owner surface had
absorbed a different slice of the recent agent waves):

- **Webview Preferences page** (`/preferences` + GET/PATCH
  `/api/webgate/preferences`) — completes the half-done owner-UX Phase 4 T3:
  schema-driven view/edit of typed prefs over the same `core.prefs` seams the
  CLI/REPL/agent use; guarded keys confirm-gated (409 → `confirm:true`);
  `WEBVIEW_READ_ONLY` blocks writes.
- **Webview Pending-review queue** (`/pending` + `/api/webgate/pending*`) — a
  web-only owner can finally approve/reject quarantined proposals; same
  `core.self_evolution` aggregator as `polyrob owner`, REPL `/pending`,
  Telegram `/approve`.
- **`polyrob finance` + REPL `/finance`** — the unified-ledger balance sheet
  (earned/spent/pending/net) was webview-only; one shared renderer over
  `build_ledger`.
- **`polyrob cron`** (`schedule/list/show/cancel`) — cron jobs were creatable
  from NO human surface (agent-tool only); rides the same `CronService` +
  `cron.db` the ticker runs; warns when `CRON_ENABLED` is off.
- **One recap vocabulary** — REPL answers `/recap` (alias of `/journey`),
  Telegram answers `/journey` (alias of `/recap`).
- **One data-home resolver** — `core.runtime_paths.resolve_data_home()`
  replaces four byte-duplicated `_data_dir()` helpers (webview pages/activity,
  cli owner/surface); webview wrapper `webgate.data_dir()` keeps the
  standalone-deploy fallback.
- **Surface-parity contract test** (`tests/unit/test_surface_parity.py`) —
  pins the capability→surface matrix (CLI/REPL/webview/Telegram) like the
  flags catalog, so a rename/removal on any surface fails CI.
- **Hygiene**: dead `chat.js` handlers for never-emitted socket events
  removed (server emits only `stream_chunk`); `POLYROB_API_BASE` replaces
  three hardcoded `127.0.0.1:9000` proxy URLs; every direct Rich print in the
  REPL handlers now routes through the secret scrub (16 sites, source-pinned).
- **Docs**: `docs/guide/cli.md` caught up (12 undocumented commands, 4 slash
  rows, the real 16-verb `owner` group); `console.md` documents
  Finance/Preferences/Pending-review; READMEs refreshed; the executed webview
  rename/align handoff archived with a status banner.

## [0.6.0] — 2026-07-12

### Chat connectors Wave 5 — X (Twitter) DM surface + connector validation hardening (2026-07-12)

- **X (Twitter) DM surface** — `surfaces/x/`: polling inbound (`GET /2/dm_events` — the
  pay-per-use tier has no DM webhook; Account Activity is enterprise) with a durable
  since-id cursor (`x_cursor.json`, first run marks history seen instead of replaying up
  to 30 days), tweepy OAuth 1.0a user-context calls run off-loop (the SAME `TWITTER_*`
  creds the twitter tool uses — no second credential store), 429 backoff to the
  `x-rate-limit-reset` epoch, dm_event-id dedup, and outbound via
  `POST /2/dm_conversations/with/:participant_id/messages` (10,000-char DM cap, split).
  1:1 conversations only in v1 (group DM conversations are skipped — replying via
  `/with/:participant_id` would leak into a private thread). Run with `polyrob x`
  (`X_SURFACE_ENABLED`; poll cadence `X_DM_POLL_SEC`, default 90s — DM reads are
  15 req/15 min per user). The `message()` tool now lists `x` among its surfaces.
- **X capability completion (twitter tool)** — new always-on reads `twitter_get_dms`
  (recent DM events, optional 1:1 participant filter — the agent could previously SEND
  DMs but never read them from a task) and `twitter_get_timeline` (a user's recent
  tweets; the internal helper existed but was never exposed, and a tweet with no
  `created_at` no longer crashes the whole timeline read); new gated write
  `twitter_unmute` (mute existed with no undo). `twitter_block`'s description now warns
  it is Enterprise-only on X API v2 (403 on pay-per-use — mute instead). All inherit the
  existing approval/rate gates, correspondent-taint blocking (tool-id match), and
  untrusted-result wrapping.
- **datagen: exports actually see real sessions now** — three layout bugs made every
  real-deployment export empty or label-less: `read_agent_steps` read `<session>/history/`
  but the live ledger is written to `<session>/data/history/` (history_io via
  `pm().get_history_dir()`) so every record exported `steps=[]`; `iter_session_dirs` only
  globbed the legacy `*/sessions/*` shape and yielded NOTHING under the canonical direct
  layout (server `data/task/<user>/<sess>`, local `<home>/sessions/<user>/<sess>`); and
  episode-label lookups assumed `memory.db` at `pm().data_root` when it lives in
  `BotConfig.data_dir` (the parent, per `backend_factory`) so every record was
  `outcome=unknown` (`find_memory_db` now resolves it; `session export` too). The batch
  runner no longer lets an assemble/export exception escape `asyncio.gather` and abort the
  whole run (counted `failed` + logged), logs the no-session/refusal branch, and its tests
  now build the REAL session layout (the old fixtures matched the buggy paths, which is
  why CI was green while production exported nothing).
- **Connector hardening (validation pass over the Wave 3/4 surfaces)** —
  **Slack:** user-id (`U…`/`W…`) send targets now go through `conversations.open`
  (cached) — `chat.postMessage` only accepts conversation ids, so a `message()`-tool/sink
  DM target previously landed in Slackbot or failed (`open_dm` was dead code);
  `not_in_channel` errors carry an actionable `/invite` hint; the Socket Mode read loop
  dispatches agent turns as tasks instead of awaiting inline (a slow turn blocked the
  NEXT envelope's ACK past Slack's redelivery timer) and skips malformed frames instead
  of tearing down the socket. **Discord:** the gateway now detects a half-open socket
  (missed HEARTBEAT_ACK → force-reclose + reconnect), answers server heartbeat requests
  (op 1), honors RECONNECT (op 7), and waits before re-IDENTIFY on INVALID_SESSION
  (op 9, IDENTIFY-rate-limit protection). **Signal:** endpoints verified against the
  native `signal-cli daemon --http` docs (JSON-RPC `/api/v1/rpc` + SSE `/api/v1/events`
  are correct — NOT the bbernhard REST daemon); the SSE unwrap now also handles the
  stdio JSON-RPC notification shape (`params.envelope`, previously silently dropped),
  an empty `account` param is omitted, and `sourceUuid` senders parse when no number
  is present.
- **Group/channel ingress is now default-DENY at the dispatcher** — with
  `GROUP_CHAT_ENABLED` off (the default), a group/channel message is silently denied
  instead of falling through to the legacy obey-path. The fall-through meant a bot
  invited into a Discord/Slack channel would obey ANY room member (those surfaces have
  no sender allowlist of their own; only telegram has `ALLOWED_TELEGRAM_USER_IDS`).
  DMs are unchanged. Group-participant coverage now also exercises the REAL
  `bind_chat_surface` write path (the `SINGULAR_CHAT_ENABLED` gate the daemons set).

### Owner-UX Phase 3 — builtin user-guide skill, generated config reference, `agent_status` config section, init guardrails (2026-07-12)

Closes the "the agent doesn't know what it is" gap: a grounded, self-referential map of
POLYROB's own surfaces/config/autonomy/money model, plus the setup and introspection
surfaces that keep it honest at runtime.

- **`polyrob-user-guide` builtin skill** (`data/prompts/skills/polyrob-user-guide/`,
  priority-1 `auto_activate`) — the map of what POLYROB is, its surfaces, the four
  configuration layers (env flags / `preferences.toml` tighten-only / `contract.md` /
  SOUL-SELF-owner-facts), the conversational `preferences` action, autonomy and
  money/safety at a concept level, skills/learning, a verbatim anti-hallucination clause,
  and a live-grounding rule (never assert a *current* config value from this static skill —
  use `agent_status`/`preferences`/`polyrob doctor --flags`). Five `references/` files
  (autonomy, surfaces, skills-and-learning, money-and-safety, setup-interview) are loaded
  on demand via `load_skill`/`read_skill_resource`, not eagerly injected.
- **`scripts/gen_user_guide_refs.py`** generates `references/configuration.md` (a compact,
  human-facing per-group env-flag reference) from `docs/CONFIGURATION.md`, mirroring
  `scripts/gen_flags_catalog.py`'s doc-walk. A drift contract test
  (`tests/unit/core/test_user_guide_refs.py`) asserts the committed reference is exactly
  what the checked-in generator produces and that every `core/flags_catalog.py` flag is
  discoverable in it.
- **`agent_status` config section** — the read-only introspection action now reports
  resolved preference values/sources (`key = value (source)`, grouped), the three posture
  axes (`compute`/`autonomy`/`local`), and which autonomy loops are enabled, all run through
  the core secret-shape scrubber as a defensive backstop. Fails soft to an explicit
  `config: unavailable` line (not silent omission) so "what's my effective config" always
  gets an honest answer, independent of the other five sections (steps/tools/context/
  wallet/ledger) that stay available even under a full orchestrator/wallet/ledger blackout.
- **`polyrob init` — "5/5 Autonomy & guardrails"** wizard section (after Owner pairing,
  before the summary; skipped by `--quick`/non-interactive): enable local mode
  (`POLYROB_LOCAL=1`), an autonomy budget (`AUTONOMY_BUDGET_USD`), the recommended approval
  preset (`APPROVAL_REQUIRED_TOOLS` + `APPROVAL_PROVIDER=interactive_cli`), and a daily
  digest channel (written as a `digest.channel`/`digest.enabled` preference when an owner id
  is known, else an `OWNER_DIGEST_ENABLED` env note) — every prompt blank-to-skip. The
  final summary now points the owner at the new skill: `Ask me anything about myself —
  try "what can you do?"`.
- **Fixes**: `scripts/gen_flags_catalog.py` no longer lets the `AUTONOMY_POSTURE`/
  `AGENT_COMPUTE_POSTURE` single-flag mini-tables' own header row (`| \`NAME\` | Default |
  What it does | Code anchor |`) win the first-occurrence dedup over their real default row
  — `core/flags_catalog.py` now reports `silent`/`0` instead of the literal string
  `"Default"`. `docs/guide/cli.md`'s slash-command table gained `/pending`, `/approve`,
  `/config`, and `/context` (all pre-existing REPL commands the table had never listed).

### Owner-UX Phase 2 — conversational preferences, contract doc, approval ladder (2026-07-12)

Closes the "settable but not yet consumed" gap Phase 1 left open — the agent can now
read/change its own tenant preferences and propose durable operating rules from inside a
conversation, guarded changes ride one owner review queue, and the approval workflow gained a
real ladder + CLI/REPL parity.

- **`ContractWriter` + `contract.md` two-tier doc** (`core/contract_writer.py`, a thin
  `SelfContextWriter` subclass) — an owner-authored/agent-proposed `## Operating contract`
  block, injected each session alongside SOUL/owner-facts/SELF, gated `CONTRACT_DOC_ENABLED`
  (default **ON**). A deterministic one-line style summary from typed prefs
  (`core.prefs.render_style_line`, covering `style.verbosity`/`style.language`/`style.tone`/
  `digest.quiet_hours`) rides the same block — no file + no style prefs set stays
  byte-identical (`""`). `CONTRACT_DOC_REQUIRE_REVIEW` (default **ON**) gates whether a
  `contract_propose` write activates immediately or lands in `.pending/`; a forged/background
  author (self-wake, sub-agent/leaf, autonomous run) is quarantined unconditionally regardless
  of the flag.
- **Agent-callable `preferences` action** (`tools/controller/action_registration.py::
  _register_preferences_action`, gated `PREFS_TOOL_ENABLED`, default OFF / **ON under
  `POLYROB_LOCAL`**) — `list`/`get` read the typed schema (effective value/source/`applies`);
  `set` writes SAFE keys immediately or queues a guarded proposal for GUARDED keys (owner
  reviews via `/pending`); `contract_propose` proposes operating-contract text. `set` is
  refused outright for any forged/autonomous turn; a leaf/sub-agent never sees the tool
  (`delegation_exclusions_for_child`); a correspondent-tainted session is denied the whole
  action. Free-text display (`get`'s description echo) is re-scanned at read time so a
  hand-edited `preferences.toml` can't smuggle prompt injection into the agent's own reply.
- **One pending/approve pipeline for guarded pref changes** — `propose_pref_change` writes a
  `pref_change` kind row into the SAME quarantine queue skills/self-context already use;
  removals are tracked by **operation** (add vs. remove), not a stale full-set snapshot, so two
  concurrent proposals against `approvals.require` can't clobber each other on promote.
- **`/approve` REPL + `polyrob approvals`** (`cli/ui/commands/h_approve.py`,
  `cli/commands/approvals.py`) — both list/add/remove the approval-gated action set through the
  SAME `tools.controller.approval.effective_approval_state()` helper `Controller.__init__` uses
  to wire the hook, so displayed state can never drift from enforcement. `add` unions a gate in
  directly (tightening needs no review); `remove` of a pref-added entry queues a guarded
  `pref_change` proposal instead (an env/posture-added entry is explained, not removable).
- **Approval ladder** (`InteractiveCLIApprover`, owner-UX P2 T5) — the old y/n prompt is now
  `o`=once, `s`=session (in-memory per-action auto-approve), `a`=always (approves + queues a
  guarded `approvals.require` removal proposal when applicable), `d`=deny, `n`=never (appends to
  `approvals.deny` immediately — tightening a denylist is always safe, no review needed).
  Missing tenant context degrades `a`/`n` to `s`/`d` with a notice rather than crashing.
- **Real `/persona` and `/toolset` switches** — both REPL commands now persist
  `session.persona`/`session.toolset` (threat-scanned, validated against `TOOLSETS`) instead of
  only showing a read-only detail view, honestly labeled "applies next session" (the system
  prompt's `<identity>` block is built once at agent-creation time, so neither can retroactively
  change the CURRENT turn); `/persona` best-effort refreshes the live `_persona_block` for
  anything freshly created within the same session (e.g. a delegated sub-agent).
- **`polyrob wallet set-cap`** — guided, confirmed CLI for setting the wallet daily/per-tx
  money caps as the **env-authoritative** values (`WALLET_DAILY_CAP_USD` /
  `AGENT_WALLET_MAX_PER_TX_USD`, upserted into `~/.polyrob/.env` — NOT the preference-write
  path; a per-user preference may only tighten below the env cap, never raise it).
- **Docs/flags** — `PREFS_TOOL_ENABLED`/`CONTRACT_DOC_ENABLED` rows in
  `docs/CONFIGURATION.md` dropped their "(reserved)" language now that both are wired; added
  `CONTRACT_DOC_REQUIRE_REVIEW`. `PREF_SCHEMA` descriptions for `style.verbosity`/`style.tone`
  dropped their "not yet consumed" markers (now rendered into the style line);
  `goals.notify_on_done`, both `autonomy.*` keys, and `budget.wallet_per_tx_usd` remain
  genuinely unconsumed and are reworded "not yet consumed — future phase".

### Memory/knowledge finalization — repairs + the knowledge layer (2026-07-12)

Implements the validated portions of the 2026-07-11 memory/context/knowledge review.

**Repairs (Phase B):**
- **Provenance sidecar (D1/B2)** — `mem_provenance(mem_rowid, user_id, ts, kind,
  content_hash)` stamped on every cross-session memory write; recall lines now render
  `- [YYYY-MM-DD] …` (legacy stampless rows stay bare). Exact duplicates collapse at
  write (refresh ts, skip insert). `core/sqlite_util.execute_retry` gains
  `fetch="lastrowid"`.
- **Retention (D3/B3)** — `MEMORY_RETENTION_DAYS` (default 365, `<=0` off): age-based
  prune of stamped `memories` rows on the curator tick; the `local_vector` backend also
  sweeps its `mem_meta`/`mem_vec` sidecar. The store no longer grows forever.
- **Row cap (D8)** — `MEMORY_ROW_MAX_CHARS` (default 4000) caps auto-injected memory
  rows at the shared FTS/vector composition point.
- **Episode artifacts (D4/B4)** — `collect_provenance` now routes through the evidence
  pack's `collect_artifacts`; episode rows carry real artifact lists.
- **H-MEM selection (D5/D9/B5)** — when finding importance is flat (the sub-prune
  regime), selection falls back to recency: the newest findings win the display slice
  instead of the oldest 15.
- **Continuity re-injection (D6)** — `_maybe_inject_autonomous_continuity` gains the
  once-per-session bootstrap guard (self-wake re-entries no longer re-inject).
- **DB manifest (D11/B6)** — `telemetry_events.db`, `surfaces.db`, `pairing.db`,
  `messages.db`, `wa_dedup.db`, `email_dedup.db` added; backup/rollback no longer
  silently skips them.
- **Dead code (B7)** — ~180 LOC of caller-less H-MEM helpers, the permanently-dead
  `knowledge_base` branch in `agents/base_agent.py`, and the dead `UserProfileManager`
  (527 LOC; its RBAC link called a method that never existed) deleted.

**Knowledge layer (Phase C):**
- **Notes substrate (C1)** — `curated_memory` promoted to first-class notes (additive
  columns: title/tags/`[[wikilinks]]`/source/timestamps/access_count/status/created_by);
  `memory` tool verbs extended to `create/update/archive/list/show` — writes threat-
  scanned fail-closed, forged/autonomous turns quarantine to `pending` and can never
  mutate active notes, note ops emit `self_modification` audit events.
- **`/knowledge` webview section (C2)** — read-only wiki: notes (incl. pending),
  episode browser (outcome/artifacts/spend), skill catalog + pending drafts, KB
  sources, and a changes tab over the durable event log.
- **Obsidian export (C3)** — `polyrob knowledge export [--out --since --user]` writes
  a markdown vault (notes with frontmatter + working wikilinks, daily episode logs,
  skills, identity docs, goals, index) — export-only projection, DBs stay SSOT.
- **Note consolidation (C4)** — `KNOWLEDGE_CURATOR_ENABLED` (local-ON): curator tick
  archives never-read agent-authored notes past `KNOWLEDGE_NOTE_STALE_DAYS` (90) and
  collapses exact duplicates. Archive-only, audited, LLM-free.

### Chat connectors Wave 3 — group-chat access + Discord surface (2026-07-12)

Chat connector wave — group-chat access model plus Discord/Slack/Signal surfaces.

- **Group-chat access model** — `GROUP_CHAT_ENABLED` (default OFF; since Wave 5 OFF ⇒
  group/channel messages are silently denied at the dispatcher, not legacy fall-through):
  only chats in a new default-DENY group allowlist (`polyrob owner groups
  allow|deny|list`, `core/surfaces/group_allowlist.py`) are served; the owner keeps the
  normal command/steer flow (mention-gated via `GROUP_REQUIRE_MENTION`, default ON); any
  other member becomes the new `GROUP_PARTICIPANT` tier whose @mentions route as untrusted
  DATA into the bound group session (the existing correspondent rail: `<correspondent-message>`
  wrap + capability taint) — participants can never command, steer, or start sessions.
  Group denials are **silent** (`RouteDecision.silent`) so channels never get auth spam.
  Fail-closed once enabled; the local-owner bypass is refused inside groups.
- **Discord surface** — `surfaces/discord/`: thin aiohttp REST client + hand-rolled
  Gateway-WS consumer (IDENTIFY/heartbeat/backoff-reconnect; GUILDS, GUILD_MESSAGES,
  DIRECT_MESSAGES, MESSAGE_CONTENT intents — no discord.py dependency), `Surface` impl
  with 2000-char splitting, dedup, typing indicator, and the shared
  `route_inbound`/`act_on_inbound` pipeline. Run with `polyrob discord`
  (`DISCORD_BOT_TOKEN`; `DISCORD_SURFACE_ENABLED`). The `message()` tool now lists
  discord among its surfaces.
- **Slack surface (Wave 4)** — `surfaces/slack/`: thin Web-API client + **Socket Mode**
  WS consumer (envelope ACKs, `disconnect` rotation, backoff — no slack-bolt, no public
  URL). DMs + channels (threads ride `thread_ts`), 4000-char split, edit support. Run
  with `polyrob slack` (`SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`; `SLACK_SURFACE_ENABLED`).
- **Signal surface (Wave 4)** — `surfaces/signal/`: thin client for a local
  `signal-cli daemon --http` (JSON-RPC send at `/api/v1/rpc`, SSE receive at
  `/api/v1/events`, backoff-reconnect), DM-first (groups parse and ride the W3 gating;
  Signal has no mentions, so mention-gated groups stay silent unless
  `GROUP_REQUIRE_MENTION=false`), min-interval send throttle. Run with `polyrob signal`
  (`SIGNAL_DAEMON_URL` + `SIGNAL_ACCOUNT`; `SIGNAL_SURFACE_ENABLED`).

### Training-data rig Wave 1 — trajectory export + capture (2026-07-11)

New top-level `datagen/` package assembles a session's persisted artifacts (message
history, step-level agent ledger, LLM usage, episode/RunOutcome labels) into a canonical
training record.

- **Formats** — `raw` (lossless), `sharegpt` (`from/value` with
  `<think>`/`<tool_call>`/`<tool_response>` conventions), `openai` (messages+tools JSONL);
  labels + provenance ride alongside so corpus filters never re-parse content.
- **Fail-closed scrub** — every export passes `core.secret_scrub` + a JSON-aware credential
  rule; a scrub failure REFUSES the record. Images stripped; sessions containing
  correspondent (third-party) messages are excluded by default.
- **CLI** — `polyrob session export --format raw|sharegpt|openai` (also fixes the exporter
  missing `memory/message_history.json`) and `polyrob datagen export --filter outcome=done`
  for bulk, label-filtered corpora (rejection-sampling-ready).
- **Opt-in capture** — `TRAJECTORY_CAPTURE` (default OFF, never in the local safe group)
  captures each finished run as a labeled record under `<data_root>/datagen/captured/`.
- **Batch rollout runner (Wave 2)** — `polyrob datagen run --tasks tasks.jsonl`: JSONL
  prompts → agent rollouts on the goals/cron session rail with per-task Bernoulli toolset
  sampling (`datagen/toolset_distributions.py`), bounded
  concurrency + per-rollout wall-clock cap, content-based checkpoint/resume, outcome-labeled
  `rollout_*.json` + merged sharegpt `corpus.jsonl` + `statistics.json` (incl. spend).
  Runner process forces trajectory hygiene (memory/project-context/autonomy off) via
  `os.environ.setdefault` so an explicit operator value still wins.

### Owner-UX Phase 1 — typed per-user preferences layer (2026-07-11)

A curated, schema-validated preferences layer so an owner/tenant can tune agent behavior
without touching env flags or restarting — `core/prefs.py` (`PREF_SCHEMA`/`validate_pref`),
stored per-tenant at `identity/{instance_id}/user_{uid}/preferences.toml`, gated
`PREFS_ENABLED` (default **ON**; inert with no `preferences.toml` present — byte-identical
legacy behavior).

- **Schema + storage + resolver** — 21 curated keys across `approvals.*`/`budget.*`/
  `goals.*`/`digest.*`/`delivery.*`/`style.*`/`session.*`/`autonomy.*`, each typed
  (bool/int/float/str/list/enum) and merge-tagged. Resolution is `pref > env > default`
  EXCEPT **guarded** keys (approvals, budgets, `autonomy.self_wake`/`background_review`),
  which merge most-restrictive (`min`/`union`/`and`/`stricter_provider`) so a preference can
  only **tighten** operator policy, never widen it. No secret-typed keys, ever. Atomic
  temp+replace writes; malformed/missing TOML fails open to `{}` (never breaks the agent).
- **Read-site threading** — goal daily-quota/concurrency, the autonomy spend budget, the
  wallet daily cap, delivery rate/daily caps, digest enabled/channel, session default
  toolset/persona, and the approval-required/deny/provider union are all resolved through
  the pref layer at their existing read sites; `approvals.provider` never lets a custom
  (non-standard) `APPROVAL_PROVIDER` be overridden by a pref. Wallet-budget note: only the
  read-side helper (`core.wallet.config.effective_daily_cap_usd`) shipped — wiring it into
  the `PolicyGate` spend-enforcement path itself is still pending. **Settable but NOT YET
  consumed anywhere** (the value round-trips through `preferences.toml`/`/config`, but no
  read site threads it): `digest.quiet_hours`, `goals.notify_on_done`, every `style.*` key,
  both `autonomy.*` keys (`self_wake`/`background_review`), and `budget.wallet_per_tx_usd`.
  Those consumers land in Phase 2.
- **Secret-guard + write-gate enforcement** — `preferences.toml` and the (Phase-2) two-tier
  `contract.md` are hard-denied to every agent file-write surface (filesystem/coding tools)
  under any `identity/` path segment, case- and segment-robust
  (`agents/task/agent/core/secret_guard.py::is_protected_config_path`); writable ONLY through
  the gated `write_preference` seam.
- **`/config` REPL + `polyrob config`** — `/config list|get|set|check` and the validated
  `polyrob config set` route a KEY to secret / per-user preference / catalog-checked env flag,
  hard-rejecting an undocumented key unless `--force`; `polyrob config check` cross-validates
  env files and a tenant's `preferences.toml` against the flags catalog, never printing a
  secret value.
- **`/context`** — a context-assembly breakdown REPL command showing what's actually being
  injected into the running session, one line per populated foundation slot with token
  count + % of context: system prompt, runtime identity, SELF_CONTEXT (SOUL/SELF),
  PROJECT_CONTEXT, initial task, skills, and conversation history.
- **Flags** — `PREFS_ENABLED` (ON), `PREFS_TOOL_ENABLED` / `CONTRACT_DOC_ENABLED` (reserved,
  ship with the Phase-2 agent-callable pref-write action and owner-facing contract doc); see
  the new "Preferences (owner UX)" group in `docs/CONFIGURATION.md`. Also closed a
  doc/catalog gap for four real, actively-read legacy env vars (`DEFAULT_MODEL`/
  `DEFAULT_PROVIDER`/`CHAT_MODEL`/`CHAT_PROVIDER`) that had no catalog row.

### `hf_deploy` — publish the workspace as a Hugging Face Space (2026-07-10)

New optional agent tool (`tools/hf_deploy/`): `deploy`/`undeploy`/`list_deployments`
publish the session workspace as a
Hugging Face Space (Docker SDK). OFF by default (`HF_DEPLOY_ENABLED`), never in
default tool_ids, gated `compute_posture_allows(ctx, 2)` (self-maintenance
tier) + owner tenant + not leaf/sub-agent/forged-turn.

- **Ship==tested acceptance-contract leg** (`tools/hf_deploy/digest.py`) — every deploy is
  refused unless the session's action ledger shows a green `run_tests` with no code-edit action
  since (reuses `agents.task.runtime.edit_verify.edited_since_last_test`); the deployed tree's
  sha256 digest is recorded alongside the live row.
- **Tenant-scoped registry** (`tools/hf_deploy/registry.py`, `deployed_apps.db` via
  `core/sqlite_util` WAL+jitter) tracks pending/approved/live/failed/undeployed per
  `(app_name, user_id)`, a per-tenant deploy-attempt ledger (`HF_DEPLOY_DAILY_MAX`,
  `HF_DEPLOY_MIN_INTERVAL_SEC`), and feeds a fail-open boot-time reconcile sweep
  (`tools/hf_deploy/reconcile.py`, wired into `core/autonomy_runtime.py`) that re-health-checks
  `live` rows and flips a dead Space to `failed`.
- **Token custody** (`tools/hf_deploy/broker.py::HFSpacesBroker`) — `HF_TOKEN` is read+stripped
  at call time and flows ONLY into the injected/lazy `huggingface_hub.HfApi`; never a param,
  result, or log line (errors are token-scrubbed). `huggingface_hub` is an OPTIONAL lazy import —
  its absence surfaces as a clear tool error, not an ImportError.
- **First-publish approval vs. approved-app redeploy** — the FIRST publish of a new app name is
  gated by a real approving provider: the tool resolves the SAME interactive-default provider the
  Controller uses at posture≥2 (`resolve_gated_actions`), so an unattended/headless run cannot
  first-publish a new PUBLIC app (`interactive_cli` fail-closes to deny). Once approved
  (registry-backed), a redeploy of that SAME app runs unattended within the caps (it skips the
  approver). `deploy` is deliberately NOT in the Controller's `APPROVAL_REQUIRED_TOOLS` sets — a
  blanket Controller gate can't tell first publish from redeploy, so the tool owns that distinction.
- Added to `DELEGATE_BLOCKED_TOOLS` and the correspondent-gate high-impact set (never delegated,
  never reachable from a correspondent-tainted session). Registered via the CLI optional-tool
  registrar (`core/bootstrap.py::_CLI_OPTIONAL_REGISTRARS`) and attached to autonomous goal runs
  only at posture>=2 (`agents/task/goals/dispatcher.py::default_goal_tools`).
- Owner runbook: `docs/guide/self-hosting.md` ("Letting the agent deploy to Hugging Face Spaces").

### Intelligence stack — outcome integrity, agent-owned communication, durable goals (2026-07-10)

Implements the approved intelligence-stack finalization (P0, v2):

- **RunOutcome envelope (§2)** — one canonical, typed outcome object assembled once at run end
  (`agents/task/runtime/run_outcome.py`); the done() text comes from the ACTION LEDGER, never from
  message-history strings. Fixes the live corruption class where an honest
  `done("OUTCOME: BLOCKED — …")` was recorded as ✅ success `"Processing actions"`; placeholder and
  generic status strings are now unrepresentable as results. Root-cause fix in
  `_extract_chat_reply` (read `agent.history`, not the nonexistent `agent.state.history`).
- **Mechanical evidence pack (§4.1) + invariants (§4.2)** — action ledger, workspace/ledger artifact
  diff (populates `episodes.artifacts`, empty in all 230+ episodes ever), final-step errors,
  ids/urls from successful results. NEW invariant: done() where every substantive action errored →
  failure.
- **One user-delivery rail (§3.1–3.2)** — `core/surfaces/user_delivery.py`: agent `send_message`
  from autonomous sessions now reaches the session's own principal (it used to die in the session
  feed); cron delivery and `push_owner_message` ride the same rail, which adds per-tenant
  content-hash dedup (24h), rate limit + daily cap, and a durable `owner_notice` fallback.
  Flags: `SEND_MESSAGE_USER_DELIVERY` (ON), `USER_DELIVERY_{DEDUP_HOURS,RATE_PER_HOUR,DAILY_CAP}`.
- **Activation fixes (§6)** — headless/CLI containers register `database_manager` (x402
  payment-request store worked never on headless); metering-only usage tracker (records real
  api_cost_usd without a credit system — ends `NO BILLING`/`Spend: $0` while $68.89 burned);
  fail-closed gate: money-enabled autonomous runs refuse to start unmetered; provider-credit
  sentinel (`CREDIT_SENTINEL_ENABLED`, ON) — one notice + dispatch/LLM-cron pause + auto-release on
  402 credit death (was: 465× 402/day, zero signal).
- **Evidence-grounded completion review (§4.3)** — `GOAL_COMPLETION_JUDGE` now defaults ON and
  judges the CLAIM against the evidence pack (no acceptance prose required): unmet → failure with
  the gap; met → verified (earns ✅ + self-wake); unclear → done (unverified), excluded from the
  learning loops (no self-wake, no inline skill distillation from autonomous sessions).
- **Typed acceptance checks (§4.4)** — optional, framework-executed, fail-closed when present
  (`artifact_glob`, `http_ok`, `register_check_type` for instance verticals); producers:
  `goal_create.acceptance_checks`, `seed_goal --check`, planner prompt. NO create gate.
- **Communication contract (§3.3) + demoted notices (§3.4)** — autonomous sessions carry a
  cache-stable `<communication-contract>` prompt block; the completion push fires only when the
  agent said nothing during the run (✅ only when verified), the blocker-escalation push only when
  the agent didn't report the block (the durable ask is always created).
- **Durable goal stewardship (§5.1–5.4)** — cold-start sweep re-queues `running` goals on boot
  without a failure increment (two deploys mid-goal used to silently block it); per-attempt ledger
  in `payload.attempts` + previous-attempt block in retry prompts (retries are no longer amnesiac);
  `goal_show` exposes acceptance/outcome/attempts; new `goal_unblock` verb (rationale-logged);
  ancient blocked goals age out visibly (`GOAL_BLOCKED_MAX_AGE_DAYS`, 14); quota exhaustion pauses
  runs, not planning.
- §5.0 (goal-module extraction behind the three contracts) is deliberately deferred until the
  contracts are live-validated and the judge's LLM provisioning stops reaching into the agent.

## [0.5.1] — 2026-07-08

Bug-fix release on top of 0.5.0.

### Money / wallet
- 2026-07-08: **Agent wallet spends from the address it tells you to fund (fund == spend).** The
  agent wallet is hub-and-spoke (one seed → per-venue keys); the x402 spend path signed with the
  `x402` venue key while `AgentWallet.address` (the owner-facing "fund me" address) returned the
  `treasury` key — so funding the surfaced address funded an address no spend path used, stranding
  funds. Now `AGENT_WALLET_OPERATIONAL_VENUE` (default `treasury`) is the venue same-chain spend
  paths sign with, `AgentWallet.address` tracks it (surfaced == spent), and a regression test locks
  the invariant. The operational venue is clamped to the fundable same-chain venues
  (`treasury`/`x402`); hyperliquid keeps its own delegated key. New **`polyrob wallet [--json]`**
  shows per-venue address + on-chain balance + network + caps and marks which address to fund
  (delegated venues are labeled "not funded here" so they can't be mis-funded). "Venue" elsewhere
  stays a policy/accounting label — per-venue caps are unchanged. Fusion-of-opuses reviewed.

### CLI / update
- 2026-07-08: **`polyrob update` apply works on a tag-pinned instance.** The git apply runner did
  `git pull --ff-only`, which fails on the detached-HEAD pinned-tag posture the instance runs (and
  would pull unreviewed `main` on a branch). It now fetches tags and checks out the resolved release
  tag for the `stable`/`pre` channels (`--channel git` keeps the branch fast-forward). Also
  `polyrob update --apply --json` no longer crashes on a failed apply (it serialized a raw
  exception); the failure payload is now valid JSON. The full apply lifecycle
  (snapshot → install → guarded-migrate → verify → auto-rollback) was validated end-to-end.

## [0.5.0] — 2026-07-08

**0.5.0 is a large capability release** on top of 0.4.3: the compute-posture ladder (installable
sandbox + persistent shell/process + `self_env`), the agent money loop, the full-control monitoring
console, restart-durable autonomy, and a broad intelligence/memory/prompt/security polish pass.
Every capability is flag-gated and a default server is behavior-identical to 0.4.3 unless a bullet
says otherwise.

### Computer-use / system-use (compute posture)
- 2026-07-07: **`AGENT_COMPUTE_POSTURE` capability ladder (0–3), default 0.** A third
  orthogonal capability axis (beside `POLYROB_LOCAL` trust and `AUTONOMY_POSTURE`
  loops): how much host/compute capability the agent has. Frozen at import (a
  mid-process env write can't raise it); garbage/out-of-range never rounds up.
  One gate predicate `compute_posture_allows(ctx, N)` — posture≥N AND owner tenant
  AND not-leaf/sub-agent AND not a forged self-wake/delegation-result turn — governs
  every posture-gated capability. A default server (`AGENT_COMPUTE_POSTURE` unset)
  is byte-identical to before. (`agents/task/constants.py`)
- 2026-07-07: **Posture 1 (`sandbox-dev`) — an installable, stateful, HTTP-testable
  sandbox.** For an entitled session: the docker sandbox mounts a writable `/install`
  (session-bound `.pylibs`), runs `python -s` with `PYTHONPATH=/install` (instead of
  the env-ignoring `python -I`, which stays at posture 0) so `pip install --target`
  imports; `run_code` gains `env` + `packages` (declarative install, network-gated);
  dev containers default to `bridge` network. A persistent **`shell`** tool
  (`shell_run`, cwd/env persist across calls; foreground/background discipline) and a
  **`process`** job manager (list/poll/log/kill) run inside the session's container.
  Container ports publish to host loopback and a narrow allowlist lets the browser/
  `web_fetch` reach exactly those ports (never RFC1918/metadata) so the agent can
  HTTP-test its own server. (`tools/code_exec`, `tools/shell`, `tools/browser`,
  `tools/web_fetch`)
- 2026-07-07: **Posture 2 (`self-maintain`) — the approval-gated `self_env` tool.**
  Distinct approvable verbs (never raw bash): `install_dep` (own venv, pinned),
  `read_source`/`patch_source` (install-tree-confined, env/config hard-denied),
  `git_pull` (ff-only, ext:: rejected), `restart_service` (supervised only). Every
  call is `compute_posture_allows(ctx,2)`- AND approval-gated and emits a
  `self_modification` audit event. At posture≥2 the Controller auto-gates
  `shell_run` + the `self_env_*` verbs behind the interactive approver (fail-closed to
  deny; headless denies). (`tools/self_env`, `tools/controller/approval.py`)
- 2026-07-07: **Self-escalation hardening.** `AGENT_COMPUTE_POSTURE`, `APPROVAL_REQUIRED_TOOLS`,
  `APPROVAL_PROVIDER` are frozen at import; the env/config files that hold them are
  hard-denied to every agent-writable surface — `secret_guard` now catches `*.env`
  (the prod `polyrob.env` basename that `.env*` missed) and adds
  `is_protected_config_path` for `/etc/polyrob`. `shell`/`process`/`self_env` are in
  `DELEGATE_BLOCKED_TOOLS` and the correspondent-taint high-impact set — never reachable
  by a leaf/forged/correspondent turn. Autonomous goal/cron runs are provisioned with
  the compute toolset only at posture≥1.

### CLI / operability
- 2026-07-07: **Flag registry + `polyrob doctor --flags` (Wave D / SA-05).** POLYROB's ~300 env
  flags are now a runtime-enumerable registry (`core/flags.py`, catalog extracted from
  `docs/CONFIGURATION.md` with a contract test keeping doc rows ⊆ registry). `polyrob doctor
  --flags` dumps every flag's resolved value + source — including live posture/local-derived
  defaults (`default(posture:owner-visible)`, `default(local=ON)`) via
  `agents/task/flag_defaults.py` — with key/token/secret values always masked. The
  "shipped dark, nobody knew" flag failure class is now visible from one command.

### Money / financial agency
- 2026-07-07: **Money-loop + wave hardening (adversarial review).** Anonymous/empty-tenant
  callers are refused across `accounting`/invoicing (an empty `user_id` previously widened the
  wallet-spend query to ALL tenants — cross-tenant financial-data leak — and created a shared
  anonymous invoice bucket); the settlement watcher claims atomically before notifying (exactly
  one wake/event per settlement under concurrent processes) and expiry never mis-reports a
  concurrently-settled invoice; `doctor --flags` masks `_SEED`/`_HASH` values
  (`PAYMENT_MASTER_SEED`, owner password hash were printed in clear) and now agrees with
  `doctor` about `POLYROB_LOCAL`; the cron wake change-gate records an outcome-tagged baseline
  so a persistently-failing gated job retries instead of being skipped as no-change;
  `autonomy_state.db` co-locates with its sibling DBs via the container data_dir and its store
  is memoized (zero sqlite I/O per session construction). Flags-catalog generator checked in
  (`scripts/gen_flags_catalog.py`, `--check` parity enforced by test); 4 documented-but-
  unregistered flags gained proper rows.
- 2026-07-07: **Money loop v1 (vision Pillar 1, flagship).** The agent can now invoice,
  get woken on settlement, and account for itself — all behind `X402_INVOICE_ENABLED`
  (default OFF): (1) new `x402_invoice` tool — `x402_request` creates a *pending*
  `x402_payment_requests` row (amount ceiling `X402_INVOICE_MAX_USD`, per-tenant daily cap
  `X402_INVOICE_DAILY_MAX`, session provenance in metadata, `payment_requested` event;
  the action is in the recommended approval set and the tool is leaf-delegation-blocked);
  `x402_invoices` lists them; `accounting` renders the unified ledger. (2) A settlement
  watcher on the autonomy-runtime ticker seam expires stale invoices and, when one settles,
  re-enters the originating session via the self-wake rail ("I invoiced → I got paid" as one
  continuous piece of work) and emits `payment_settled`/`payment_expired` events; settlement
  is an attested transition (`polyrob owner settle <id> [--tx-hash]`, plus `owner invoices`).
  (3) `modules/credits/unified_ledger.py` — one read-only view joining LLM/tool costs
  (`usage_records`), wallet spend (`wallet_spend` events), and x402 receipts/pipeline:
  earned / pending / spent / net, evidence-backed and tenant-scoped. Agent finances stay
  separate from platform billing; every leg is fail-open.

### Autonomy
- 2026-07-07: **Continuity on + restart-durable autonomy (vision Pillar 4).**
  (1) `AUTONOMY_POSTURE` owner-visible/full now also turns on the continuity/learning trio
  that was local-only dark on the server: `EPISODIC_MEMORY_ENABLED`, `EPISODIC_DIGEST_INJECT`,
  and `REFLECTION_ON_SESSION_CLOSE` (now posture-governed via
  `AutonomyConfig.reflection_on_session_close`; explicit env always wins).
  (2) The two volatile autonomy registries persist to a new `autonomy_state.db` sidecar
  (WAL+jitter, registered in `core/db_manifest.py`), gated `AUTONOMY_STATE_DURABLE`
  (default ON, fail-open): background delegations write dispatched/terminal rows and a
  startup sweep (`core/autonomy_runtime.py`) marks crash-interrupted delegations
  `interrupted` and surfaces them back to their session via the self-wake rail — never a
  silent evaporation, never a magic resume; the self-wake `ReentryBudget` depth cap now
  survives restart (a mid-storm loop can't get a free reset by crashing), with stale rows
  aged out and per-session ids seeded past persisted history.
- 2026-07-07: **Wake change-gate (vision Pillar 3).** A cron review job with
  `payload.change_gated` now skips the paid model call when nothing observable changed since
  its last tick — a cheap fingerprint over the tenant's goal board/events, other cron runs,
  and newest episode is compared to the per-job baseline in `cron.db::wake_gate`
  (`cron/wake_gate.py`); an unchanged fingerprint is a $0 tick (`cron_run skipped/no_change`),
  the fix for the observed ~23/25 no-op review-wake economy. Delivery jobs are never gated and
  every fingerprint error fails open (the tick runs). Gated `WAKE_CHANGE_GATE` — default OFF,
  ON under `AUTONOMY_POSTURE=full` (it pairs with `CRON_ENABLED`); explicit env always wins.

### Console / Webview
- 2026-07-07: **Full-control console: one data root, all sessions, in-process interaction.**
  (1) RC-1: the webview installs its process-global `pm()` from the shared resolver
  `core/runtime_paths.py::resolve_session_data_root()` (`DATA_ROOT` wins →
  `{POLYROB_DATA_DIR}/sessions` → legacy `./data/task`) at startup, so the console reads the
  SAME session tree the agent writes (prod previously browsed a stale `/opt/polyrob/data/task`
  while the agent wrote `/var/lib/polyrob/sessions` — catalog, feeds, and the /activity
  feed-watcher/telemetry tail were all wrong). (2) RC-2: in own_ops/local the owner's catalog
  lists sessions across ALL user dirs (CLI=`local`, telegram=`u_<hash>`, …) with a per-row
  user chip; own_ops non-owner identities get `[]`; multitenant stays strictly per-tenant.
  (3) WS-3: `POST /api/session/{id}/messages` and queue-status call the IN-PROCESS task
  router/TaskAgent when mounted (prod is single-service; the legacy `:9000` proxy remains the
  two-service fallback); the directly-mounted `/api/task/*` routes gain a read-only mutation
  guard and are pinned non-public; posture `local` now stamps the canonical owner auth state
  (the loopback operator IS the owner) so the local console can create sessions instead of
  402ing. (4) WS-4: active catalog rows carry an honest runtime chip via the P6 routing seam
  (`live@agent` in another process / `live` here / idle), and a remote-owned send returns an
  honest 409 instead of a false 404.
- 2026-07-06: **UI/UX finalization (rendered-page evaluation fixes).** Socket.IO now accepts
  the console's own serving origin (bind-port origins in the default allowlist + a true
  same-origin gate that never trusts JS-settable `X-Forwarded-*` headers) — live streams work
  from any local origin instead of dying with engineio 400s. `/settings` probes for the
  separate API service once and renders an honest "needs the POLYROB API service" state
  (crypto-trading cards only render when their tools answer; the Preferences/API-Keys
  "Coming soon" stubs are gone). Tenant nav (Profile/Sign In) no longer leaks into
  local/own_ops pages (posture-aware layout default). The System page's memory-backend header
  and doctor output flow through one resolution and can't contradict each other.
- 2026-07-06: **`/activity` daily-driver polish.** Day-separator rows + full-timestamp
  tooltips; goal events enriched with the goal's title (cached fail-open goals.db lookup) and
  outcome/status so dispatcher start/done pairs read start→done; kind-filter chips collapse
  behind "+N more" past 8 kinds; the session drill-down panel shows summarized feed lines
  with status coloring and live tail-follow; a reconnect hint appears when the rejoin
  snapshot can't cover the gap.
- 2026-07-06: **Page polish.** Memory page captions results ("showing the N most recent" /
  "N matches") over a structured `{items,count,mode}` API; identity page probes `/pfp.json`
  once instead of firing a 404 chain for avatar-less instances; session catalog rows
  deep-link to the Feed tab (`#feed` hashes now honored) with an SVG empty state; chat empty
  state gains an orientation hint; read-only consoles render a monitoring hint instead of
  dead model/tools pickers.
- 2026-07-06: **Global `/activity` terminal.** New console page streaming everything the
  instance does live — every session's feed events (steps, tool calls, LLM calls, lifecycle)
  plus goals/cron/telemetry/skill events — with kind/text/session filters, follow-tail,
  per-event JSON unwrap, and per-session drill-down panels. Cross-process backbone
  (`webview/activity.py`): recursive `watchfiles` over the session data root + id-cursor tails
  over `telemetry_events.db`/`goal_events`/`skill_install_audit`, one normalized event shape,
  Socket.IO room `activity`. Owner/admin-gated in every non-local posture
  (`WEBVIEW_ACTIVITY_ENABLED`, `WEBVIEW_ACTIVITY_TAIL_SEC`).
- 2026-07-06: **Display gaps closed + bug fixes.** The rich per-event Feed renderer is finally
  reachable (the session view was missing its Feed tab button); Stats now shows the computed
  provider-cost/markup breakdown; `POST /api/internal/emit` emits to the room clients actually
  join (was a dead `session:`-prefixed room); `/api/repair/{id}` runs the REAL
  `repair_sessions.repair_session_telemetry` (was fake success); duplicate startup handlers
  merged; dead `compute_feed_checksum`/shadowed duplicate stream route removed.
- 2026-07-06: **Security hardening.** Owner-login gains a per-IP attempt throttle (5/5min →
  429) and stateless double-submit CSRF; `return_to` open redirect neutralized; new enforced
  `WEBVIEW_READ_ONLY` mode (mutations 403, chat input hidden) for monitoring-only deploys.
- 2026-07-06: **Standalone VPS deployment shape.** `deployment/polyrob-webview.service`
  (loopback bind, `--forwarded-allow-ips=127.0.0.1`, env from `/etc/polyrob/*.env`) +
  `deployment/nginx-webview-ownops.conf` (TLS + websocket proxy) + `scripts/deploy_webview.sh`
  (backup → rsync → install → verify). Dead `webview/deploy.sh` (port-3000/`/opt/rob` era)
  removed; `webview/README.md` rewritten to match reality.

### Earning & owner experience
- 2026-07-08: **Payable endpoint + financial visibility + owner continuity.** A payable x402 invoice
  endpoint with correspondent-rail settlement delivery; a webview **financial dashboard** over the
  unified ledger; a **deterministic ($0, no-LLM) owner daily digest** over the ledger + event log; a
  bounded **owner-facts doc** on the SELF/SOUL seam. CLI: **`/journey`** + `polyrob journey` (a
  did/learned/earned/changed timeline), **`/learn`** (distills a described procedure into a
  quarantined skill), and **`polyrob init`** (pairs an owner + instance id; `doctor` reports the
  pairing). A crash-interrupted running session now **resumes on the next message** (durability
  documented). A Fusion-panel review closed reachability / resume / fund-safety / spam / quarantine
  gaps. All flag-gated OFF by default.

### Intelligence, memory & prompts
- 2026-07-08: **Intelligence-layer polish (P0/P1/P2 waves).** A broad correctness pass over the core
  agent loop and its memory/prompt/context/aux seams: core-loop fixes (P0-1..7); automatic prefetch
  no longer self-echoes the current session and skips sub-agents; reflection/forgetting is no longer
  disabled when `phase=None`; reflection summaries are capped + threat-scanned before a durable write
  and now actually reach the cross-session store; one-shot ephemeral context is consumed on success
  and restored on failure; the compaction cooldown isn't stamped on a no-op/aborted compaction and
  the pre-synthesis placeholder brain never persists to history; the background reviewer/judge aux
  clients are provisioned off the event loop and closed instead of leaking a pool per fire;
  autonomous sessions default to prefetch cadence 3. `HMEM_TAIL_PLACEMENT` now defaults **ON** (H-MEM
  rides the cacheable tail). Prompt pass: valid brain-state JSON + accurate rules + an agency
  charter, a compact `<available-actions>` index in native mode, identity-precedence/owner
  unification, a true turn-exit contract, de-feared delegation, and browser/vision/input-format/
  MCP-fallback guidance gated on the session's real tools; the per-step brain-state format nag is
  family-gated. One persona resolver across all surfaces.

### Self-evolution & skills
- 2026-07-08: **Self-tooling + skill-authoring safety.** The orphaned self-tooling path is wired as
  **`mcp_install`**; a full-body `show` verb precedes promote/reject; skill promote preserves
  original authorship and is **owner-only** (not merely non-forged); a fallback-loaded skill
  registers into the session skill set; keyword matching is **word-boundary anchored** (no substring
  false positives). The REPL gains **`/pending`** — an owner review queue for agent self-evolution.

### Telemetry & observability
- 2026-07-08: **First-class events to the durable event log.** `memory_recall` / `memory_write`,
  `self_modification`, and `goal_run` are now first-class telemetry events — learning, self-edits,
  and autonomous goal runs are observable after the fact instead of inferred from logs.

### Security
- 2026-07-08: **Untrusted-data & gating hardening (P1 wave).** A real gated-skill load gate +
  external-skill scan; untrusted content offloaded to workspace files is framed as DATA; delegation
  results are wrapped and their wake-kicks bounded; the correspondent capability-gate now covers
  money / egress / exec verbs; the email surface can't fall through to the obey-path when the tier
  model is off; curated-memory reads are wrapped as untrusted DATA.

## [0.4.3] — 2026-07-06

### Tools
- 2026-07-05: New agent-callable `message` send tool (behind `MESSAGE_TOOL_ENABLED`, default
  off, ON under `POLYROB_LOCAL`) with an owner-scoped outbound allowlist — every non-owner
  target is denied by default until the owner allows it (`polyrob owner allow/deny/allowlist`,
  or the Telegram `/allow` verb).

### Autonomy
- 2026-07-05: **Goal completion verification (intelligence-first).** Goals can now honestly fail:
  the goal-run prompt teaches `OUTCOME: BLOCKED — <need>` and a declared BLOCKED routes to the
  failure/escalation rail with an immediate block (retries are pointless when the agent itself
  says so; owner cancel always wins). An optional **completion judge** (`GOAL_COMPLETION_JUDGE`,
  default off) has a cheap aux model verify `payload.acceptance` against the framework-recorded
  action ledger — `unmet` fails the goal, uncertainty always passes. Deliberately NO
  string-matching side channels: an earlier refusal-scan + hardcoded capability-notes layer was
  removed the same day (owner directive — platform/capability knowledge lives in the agent's
  memory/skills/mission content, not framework code).
- First-class **asks**: when a goal blocks or the planner leaves the pipeline empty, the agent now
  leaves a durable "I need X from you" ask on the goal board (behind `GOAL_BLOCKER_ESCALATION`);
  fulfilling one (`polyrob owner fulfill <id>`) flips its blocked goals back to ready.
- Empty-pipeline stalls now escalate to the owner once per stall after
  `GOAL_EMPTY_PIPELINE_ESCALATE_AFTER` consecutive fruitless planner runs (a "queue healthy"
  verdict never escalates).
- Telegram owner-admin verbs: `/pending`, `/approve <id>`, `/reject <id>`, `/asks`,
  `/fulfill <id>` — the self-evolution approve loop and the ask queue are now reachable from a
  phone, not just the CLI. Owner-gated by principal; no local bypass on network surfaces.
- New CLI verbs: `polyrob owner asks`, `polyrob owner fulfill <id>`.

### Skills
- New `x-engagement` bundled skill: write-side X/Twitter engagement playbook (route selection,
  quality bar, live-URL completion proof; documents that cold replies AND cold quote-tweets are
  403-blocked for automated accounts).

### Fixed
- CI: removed five test modules that imported private (non-exported) helper scripts and broke
  test collection on a clean checkout (`tests/unit/test_battletest_metrics.py`,
  `tests/unit/test_seed_battletest.py`, `tests/unit/test_seed_cron_outreach.py`,
  `tests/unit/memory/test_e1_harness_smoke.py`, `tests/unit/memory/test_e2a_harness_smoke.py`).
- Goal completion judge: dedicated tolerant judge-response parser plus one corrective retry, so a
  chat model that narrates instead of emitting the verdict JSON no longer masks verdicts via
  fail-open.

## [0.4.2] — 2026-07-04

Initial public release. POLYROB is a self-hosted autonomous AI agent that pursues goals, learns
from experience, and runs entirely on your own machine.

### Agent core
- Autonomous task loop: give it a goal in plain language and it plans, browses the web, reads and
  writes files, runs code and shell commands, calls tools/APIs, and recovers from its own errors.
- Multi-provider LLM — OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter, NVIDIA NIM — behind a
  native LLM layer (no third-party agent framework), with automatic failover and live model
  switching (`/model`), prompt caching, and optional extended thinking.

### Memory & learning
- Cross-session recall: SQLite FTS5 keyword search by default, or optional hybrid keyword+vector
  recall (`sqlite-vec`) that degrades gracefully to keyword search.
- Reflective, hierarchical memory with importance-based forgetting; an episodic activity log that
  bridges new sessions.

### Autonomy (personal-agent mode, `POLYROB_LOCAL`)
- Durable goal board (SQLite, atomic claims, circuit breaker) that survives process restarts.
- Natural-language cron with out-of-band delivery; self-wake; background review; a skill curator.
- Self-written skills through a scanned, quarantined pipeline; every self-modification is reviewed
  before it takes effect. Skills use the open [agentskills.io](https://agentskills.io) `SKILL.md`
  format.
- Least-privilege sub-agent delegation (`delegate_task`), sync or detached.

### Interfaces & interoperability
- Terminal agent (`polyrob`), single-user web dashboard (Socket.IO), REST API with SSE streaming,
  and a drop-in OpenAI-compatible `/v1` endpoint.
- A2A (Agent-to-Agent) protocol, MCP client (STDIO/SSE/HTTP/Streamable HTTP), and chat surfaces:
  Telegram, email, WhatsApp.

### Tools
- Lightweight `web_fetch` (URL→markdown, no browser) and full Playwright browser automation;
  structured web data (AnySite), Perplexity search, coding tools, and opt-in code execution.

### Safety (on by default)
- Untrusted-input wrapping, least-privilege delegation, schema sanitization, and SSRF confinement.
- Three-tier access control (OWNER / CORRESPONDENT / DENIED) for chat surfaces, with a capability
  gate for correspondent-tainted sessions; optional memory threat-scan.

### Optional crypto/web3 (off by default, unaudited)
- x402 pay-per-request, a native agent wallet with spend caps, and ERC-8004 agent identity. This
  code has not had an independent security audit — see [SECURITY.md](SECURITY.md).

### Deployment
- Self-hosted, MIT-licensed. Modular install extras (`server`, `browser`, `memory-vector`, `crypto`,
  `telegram`, `twitter`, `voice`). Three deployment postures (local / own_ops / multitenant) and a
  Docker Compose setup.

[0.4.2]: https://github.com/theselfruleorg/polyrob/releases/tag/v0.4.2
