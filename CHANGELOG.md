# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

This is the full technical record. Entries use only the six Keep a Changelog
headings and describe shipped behavior; the rules are in `RELEASING.md`. The
short, user-facing notes for each release are on its GitHub Release page.

## [Unreleased]

## [1.7.0] — 2026-10-29

### Added

- Operational self-awareness: `agent_status(tool=)` says why a tool is missing, who fixes it and when;
  `propose_setting` gives the owner a one-tap fix card; `http_request` writes JSON over HTTP; `/update now`
  skips the owner-quiet wait.
- Console Work › Goals: a goal board with one lane per live status, the open decisions, dependencies, sub-goals,
  cost against the run limit and a detail with runs and history; actions use the existing goal verbs and the
  Inbox decider; the goal form takes `depends_on`.
- `wallet_sign`: the agent signs a site's login or challenge text (EIP-191, SIWE, SIWS) once a classifier, re-run
  in the signer, binds it to the site; permits, orders and blind bytes refuse; site keys are stored encrypted.
- Trading mandate: one owner card (`/trade start`) starts a hunt, a daily review and an exit job per position;
  code arms and cancels exits, $0 prechecks watch, entry-only breakers latch, `/trade status|pause|stop|release`.
- Discovery reads say ok / empty / unavailable, and `token_info` carries survivability fields with a tier ceiling
  (missing is unknown, one tier lower); `/book retire` and `defi_data.retire_row` close a measured zero or dust row.

### Changed

- Operator MCP servers belong in `~/.polyrob/mcp.json`; `config/mcp_config.json` ships defaults only, and a
  deployed tree copies its servers there once at startup.
- DeepSeek is a native provider: a `DEEPSEEK_API_KEY` alone runs the agent on DeepSeek's own API (`deepseek-flash`,
  `deepseek-v4-pro`) with tool calls, thinking-mode reasoning replay, cache hits and the peak-hour price; the
  bespoke client, its adapter and the old same-role message merge are removed.
- No default chain: every `defi_trade`, `defi_data`, `dapp_browser` and `agent_nft` verb requires `chain`, the wallet,
  identity and LP CLI commands require `--chain`, and `/nft`, `/identity` and `/deploy` require `on <chain>`.
- A trading-mandate entry costs at most its promotion stage's ticket ($1 probe, then half, then full) and needs an
  exit plan before any quote; the status snapshot and the digest gain a $0 Trading block (positions, exit coverage,
  breaker, realized P&L with close reasons, LLM cost).
- After a chat turn's first reply-only step the agent is told the reply was delivered (finish with `done()` or keep
  working), so it stops repeating a reply in the extra step the conversational-exit allowance grants.
- Solana swaps write the position book, a major↔stable swap books P&L, one $1 dust line is shared, and the
  `exits`, `treasury-trading`, `sizing-and-risk` and `position-journal` skills lose their job templates and ledger text.

### Fixed

- The agent gets the true cause: a tool that cannot run is gated with its remedy, `load_tool` returns the
  recorded reason, the retry wall quotes the last error and resets per owner turn; `/mcp add` works.
- `wallet_sign`: one call fetches (POST/GET), signs and submits with the challenge's cookie; an unsent signature
  says so and a second one refuses; SIWE without expiry, nonce-only sites, sibling hosts and disclaimers sign.
- `cronjob_edit` no longer reports a value the job already has as an edit: a call that changes nothing now refuses
  with the way to add task text (old_text and new_text), so an agent cannot loop on a no-op "Edited cron job".

- `twitter_get_mentions` returns each mention's `author_id` and `author_username`; without them the model guessed
  an author and tagged the wrong account in a reply.
- `wallet_holdings`, `wallet_activity` and `token_origin` list `chain` as required in the schema the model sees (a 0x address
  needs it), so a model stops omitting it; in-process calls for base58 addresses are unchanged.
- The controller's retry key treats an omitted argument, an explicit `None` and stray whitespace as the same call, and the
  retry-wall refusal carries a `retry_wall` flag instead of being recognised by its wording.
- A step made only of "Maximum retries exceeded" refusals now counts as a failed step, so a run that keeps re-asking for a
  dead operation stops after `max_failures` such steps instead of burning its whole step budget.
- `defi_data wallet_holdings` on a 0x address with no chain now answers with a ready-to-copy call naming a chain, so a
  model stops repeating the chain-less call that cost a 150-step run.
- A short `message` caption no longer hides an earlier `send_message` text from the repeat-reply guard: the guard now
  checks every text the turn spoke, so the owner stops receiving the same reply twice.
- The CLI no longer stops with `PermissionError` when it runs from a directory its user cannot read (the retired
  key-backfill hint stats a relative `config/.env.*`).
- Every X tweet read carries `reposts_and_quotes` (retweets + quotes, the figure X's app shows as reposts), so a
  repost goal no longer reads the retweet count alone and keeps pushing a target it has already met.

### Removed

- `polyrob service status` no longer checks the systemd unit for the pre-1.4.0 `RestartForceExitStatus=75` hint.
- The NFT `holder` tier and its token gate: the Collab.Land and Alchemy check tools, admin `verify-token`,
  the `den_token_count` and `requires_den_token` API fields, and six flags. A `holder` row migrates to `free_access`.
- The one-goal `/trade` verb: `/trade` now opens and runs the trading mandate.

### Security

- Dependency floors for new advisories: `certifi>=2026.10.19`, `pillow>=13.0.0`, `pypdf>=6.21.2`; the lock also
  takes the fixed `filelock`, `msgpack`, `opentelemetry-api` and `setuptools`.

## [1.6.0] — 2026-10-20

### Added

- The agent continues by itself once the owner has decided every card it raised: one wake per card group with each
  card's outcome; a quote, extra-check or recovery card stays in the group; a failed confirm offers its extra check as
  a card, and Refresh works on an expired quote.

### Changed

- Goal, cron, delegated and worker runs stop at one operator step cap, `AUTONOMOUS_MAX_STEPS` (150), and at
  `AUTONOMOUS_RUN_BUDGET_USD` ($2.00) of spend, sub-agents included; at the cap they report once, and a run that stops
  before `done()` names why in its record, the CLI and the session status.

### Fixed

- The interactive-terminal log formatter interpolates %-style arguments instead of printing a literal `%s`.
- The money simulation no longer runs a built transaction at its placeholder 120k gas limit: every aggregator swap
  ran out of gas in simulation ("execution reverted", data 0x) and was refused.
- The owner-thread delta keeps the newest rows and names the rest; the owner's commands, card taps and their results
  no longer read as the agent's own or an autonomous run's; a pinned stablecoin keeps its peg when one source is
  outvoted.

### Removed

- The `max_steps` argument of `goal_create`, `delegate_task` and `worker_manage`, the console goal form's step field,
  `polyrob workers new/edit --max-steps`, `polyrob cron edit --max-steps` and the `GOAL_DEFAULT_MAX_STEPS` flag.

### Security

- `aiohttp` is raised to 3.14.4 or later (locked 3.14.5) for the keepalive-timeout, unbounded-memory and Secure-cookie
  advisories.
- A payout-shaped call or endowed deploy needs the owner tap on every lane, a seed in a readable `.env` refuses host
  code runs, owner money jobs need a card, and x402 books the signed amount.

## [1.5.0] — 2026-10-18

### Added

- `polyrob job edit --tools a,b,c` sets a job's explicit toolset (`payload.tools`, `none` clears it), validated against the
  core and first-party pack tool ids, so a posting job can be kept off the spending tools.
- A cron job or goal write that pins an unknown skill id is refused (the lane gate's one validator); a pinned skill missing at
  run time is named in the first line of the run's instructions.
- A run record's `meta` says how its skills loaded: `skills_auto` (pinned at session start) beside `skills_loaded`, and
  `skills_regime`; the rest were loaded with `load_skill`. Measurement only: nothing retires a skill.
- Bundled skills are seeded into `<data_home>/skills/bundled/` and served from there: a fix is the agent's pending draft the owner
  accepts, with no deploy; an update replaces only unmodified copies and its report names the kept ones.
- Every shipped skill has a `metadata.polyrob-when` line (at most 60 chars) that the skill index shows after its id; a skill write
  with a longer one is refused, and a test checks the shipped library.
- A skill change ledger in `skill_usage.db` (action, actor, before/after sha, archive path, `absorbed_into`); archived bodies are
  content-addressed; `rollback_skill` restores one through the write gate; a background or curator delete names its absorber.
- `tests/golden/prompt_surface/` holds the system text and `tools[]` the model receives on eight surfaces (owner Telegram DM and
  REPL, cron, goal, room, correspondent, leaf, prod-posture owner DM) with sizes per layer; a prompt change regenerates it: `python devtools/gen_prompt_surfaces.py`.
- `/job stop <id> --forever` stops the job's lane (pinned skill, else rig): no non-owner job there until `/job allow`; a per-turn
  `<operations>` block shows live jobs, goals, pauses and stops and outranks owner.md and recall; owner.md promote rebases.
- `/usage [days]` on every seat (spend by model, kind, job and day, cache %, $ per useful run) and a "Top AI cost" digest line; a cron
  `payload.precheck` skips a quiet watch tick with no model call; `model_call_quality` and `prompt_manifest` events; `polyrob doctor` billed-vs-catalog line.
- `HEAVY_MODEL`: a money-class cron job or goal (`priority=money`, or a money tool in its toolset or rig) that pins no model runs on this
  model on the pinned provider, through a `model_tier` column of `REGIME_POLICY`, so a box keeps a cheap default for chat and routine runs.
- `/inbox` reads every decision list (open cards, signer approvals, agent-written jobs and goals); `/pending` is its view and the console API
  agrees; `/missed` names the cap only when the cap was the cause.
- Telegram and the console gain `/model` (the model, each pin and its source; a write goes through the config oracle), `/skills` and
  `/config get|explain`; the REPL-only verbs are rows of `core/verbs.py` (`seats=("repl",)`). The `/approve list|add|remove` alias is removed.
- `agent_status` (health, next runs, release notes) is on every posture; `AGENT_STATUS_TOOL` gates only wallet/ledger/config. The identity
  names `POLYROB <version>, instance <name>`; episodes keep `meta.skills_loaded`; the guide skill gains a generated `references/verbs.md`.

### Changed

- `cronjob_edit` reports the job's next run, and both it and `cronjob_show` name any other live job firing at the SAME
  minute, so a judge meant to run "24h after the announce" cannot silently share the announce's schedule.
- A `prompt_manifest` row carries `estimate_gap` (provider `prompt_tokens` minus the slot estimates); the deploy log prints
  the bundled-skill seed lines; `knowledge_export` moves from `core/` to `agents/task/` (no upward edge).
- `skill_manage create` needs a `when` index line (<= 60 chars, written into the frontmatter); the skill writer and the REST
  skill API no longer write keyword triggers into a tenant rule.
- `skill_manage` `patch`/`edit`/`delete` carry the sha256 `load_skill` returns (a stale one refuses with the current sha); `create`
  never overwrites; new `edit` and `rollback` actions; the scan and lint check only the lines a write adds.
- `treasury-trading` fits the pin budget: a 19K core with the NO ROUTE rule in its head; the chain table, execution order, NFTs and
  learning-the-meta move to `references/`. A test holds every shipped skill body to 20,000 chars.
- Skills load on purpose: only pinned skills (a job's or goal's `payload.skills`, persona seeds, their `requires`) load at session
  start; every other session gets a short index and calls `load_skill`. The keyword matcher and per-turn re-match are deleted.
- One block owns tool status: with `<tool-catalog>` pinned, its lines carry each gate (flag, unmet posture limb) and
  `<tool-availability>` keeps only the goal line; an owner seat pins the catalog without `POLYROB_LOCAL`.
- A session resumed for a correspondent runs on `filesystem`, `task` and `message` instead of the ambient grant
  (75 to 39 tool schemas): the capability gate refuses every high-impact tool there anyway.
- Owner chat runs on the cron shape: the `owner` rig and granted money tools eager, `<tool-catalog>` + `load_tool` always,
  the `tool_call` bridge, one catalog / load / not-loaded text on every seat; prod-posture owner DM 58K to 39K tokens.
- An active user skill's reviewed revision lives in `skill_usage.db`, not the tenant `rules.json`; a legacy hash there
  still verifies until the skill's next write moves it.
- The system prompt's prose moved out of `prompts.py` (1,467 → 1,062 lines) into `data/prompts/system/<block>.md`, read by `core/prompt_text.py`;
  Python keeps the conditions and the order; each block has a size ceiling, and the owner-facing texts pass the glossary.
- A skill's SKILL.md frontmatter is its only metadata source: the builtin and pack `rules.json` files are gone,
  the rule index is built from `metadata.polyrob-*` (the `tool_ids` gate, priority, auto-activate, and the new `polyrob-requires`).
- `<owner-instructions>`, the standing-goals line and the live-state line render only on an owner seat (owner chat, and live state on
  autonomous runs), never in a room, a correspondent chat or a sub-agent; the prompt golden's leaf is built as `delegate_task` builds it.
- The session's workspace path left `<environment>` for its own foundation row, and it and the task now come after the skill and tool
  catalogs, so two runs of one cron job or goal share the cached prefix through the catalogs.
- One gate text per tool: `core/tool_gates.py::REMEDIES` holds every remedy, a gate row (core or `pack.toml`) names a key,
  and `<tool-availability>` and `<tool-catalog>` render the same text; the trading venues no longer say "never autonomous".
- One home for the skill catalog: `core/skills` holds frontmatter, scopes with one `resolve` (two non-builtin skills
  with one name and different bodies are refused) and the gate predicates; the writer and migration move to `modules/skills`.
- The system prompt no longer lists every action in `<available-actions>` (about 3K tokens): in native tool mode `tools[]` is the one list;
  the JSON fallback keeps it, and the action index lists an aliased action once.
- The system prompt states each rule once (done vs send_message, the memory field, paths, live state; pause and work only in `<operations>`),
  names `web_fetch_fetch_url` and no `polyrob …` command, and calls a model-written rule a draft; the SOUL template drops its approval lines.
- Agent steps: OpenRouter sends `tool_choice=required`; an empty completion retries in the call; a tool name is repaired; an autonomous
  run that acted ends on a text reply; owner chat compacts on an 80k history budget and keeps the current request; tool disclosure is on for autonomous runs.
- One session-regime table (`REGIME_POLICY`) decides pinning, recall rank, owner memory and run records per session kind;
  `OWNER_THREAD_TAIL_HOURS` defaults to 24; memory filter v4 drops step narration (the purge reruns, backup first).
- Jobs: `/jobs` (next run, last outcome, author) and `/job new|show|edit|pause|resume|run|stop` on every seat, `polyrob job …` (`polyrob cron` an
  alias for one release); a paused job shows no next run; a stop records its seat; one scheduler-off sentence; `/cron` is removed.
- Goals: `/goal new|show|retry|pause|stop [--forever]` on every seat (`/goal ready|resume|cancel` deleted); `paused` is a status; stop is once unless
  `--forever`, the same on chat, CLI, console and `goal_cancel`; console Work lists stored goals and jobs when off.
- `CHANGELOG.md` ships in the wheel (`core/CHANGELOG.md`); on a deploy `release_notes('latest')` leads with `[Unreleased]`; the guide and
  its skill explain standing work as job / goal / decision / pause with one which-one table, and retire rail, objective and stream.
- Status names the next 5 runs or why none runs, and says IDLE (not RUNNING) while autonomy is off; a blocked
  goal names its error; runtime cost names its window; digest, `/recap`, `/journey` name a failed read.
- The holdings read values the native coin as money: its row shows the USD value and what is spendable above the chain's fee reserve (`ChainRow.fee_reserve`, e.g. 0.002 ETH on an L2), and a `total incl. native` line counts it; an unpriced native is named, never counted.

### Removed

- Dead skill code: the `action_names` trigger (no shipped rule could fire on it), `SkillContext`, the second frontmatter parser,
  the unused scope list, `scripts/skills_frontmatter_sync.py` and the `adopt` keyword. The guide states the real skill precedence.
- The unread prompt constants in `agents/prompt` and every mention of the retired `OWNER_RULES_IMMEDIATE` env in code comments and tests; the docstrings now say a model-written rule is a draft the owner confirms.
- Text-guessing gates: read taint, refusal taint, the financial-claim gate, `authored_by`/`/adopt`, the memory write filter and cron's
  job-text regexes. Unattended money needs a non-`supervised` money regime at the verb; 4 flags retire.
- One way to pick the model: `CHAT_PROVIDER`/`CHAT_MODEL` retire to `DEFAULT_PROVIDER`/`DEFAULT_MODEL`, `COMPACTION_AUTO_AUX` to
  `AUX_AUTO`; `POLYROB_<PROVIDER>_MODEL` is no longer read. `polyrob doctor` names each retired line still set.
- Rails, objectives, streams and the planner: `/rail`, `polyrob rails`, `/goal objective`, the `streams`/`planner`
  pause scopes, `polyrob owner pause-streams`; 11 flags retire. A recurring need is a scheduled job.

### Fixed

- A 1.4.0 server data home (`<POLYROB_DATA_DIR>/data`) moves up into the data home at first start, under a lock;
  nothing is merged, overwritten or deleted, and `polyrob doctor` names what stayed. Before, the upgrade opened an empty `bot.db`.
- `propose_action`'s `replaces` closes only the owner's own proposal cards, never a quote waiting for Confirm, a choice
  card or another user's card.
- Telegram `/model` checks both halves of the pin before it writes either, so a higher layer that pins only
  `DEFAULT_MODEL` no longer gets a provider of another model family.
- REPL `/usage` still shows this chat's usage when the data home cannot be read; the spend view says "could not read".
- Console Inbox Approve/Reject on a signer approval goes to the signer's decider (it answered "unknown pending kind").
- `/goal stop <id> --forever` typed with a phone dash (`—forever`) switches the goal off; `/goal retry` help says it
  re-queues a blocked or paused goal, and a pause then retry no longer runs two copies of the goal.
- `python -m migrations.migrate status` reads the same `bot.db` that `upgrade` writes.
- The "MCP is off" refusal names what the owner sets, not an internal key.
- Rolling back a bundled skill after an update works; accepting a bundled fix no longer deletes a newer draft, is refused
  when its ledger row cannot be written, and a partial first seed keeps serving the packaged library.
- Recap and the daily digest say the episode store could not be read, instead of reporting no activity.
- Console job create answers a named 503 when the job store cannot be read; a job edit no longer loses a pause or an
  outcome written at the same time.
- REPL `/skills <query>` filters on the whole query, as Telegram does; `polyrob-requires` accepts a JSON string, a YAML
  list or a number/null.
- Memory: while the store is full, the retry queue keeps only the newest summaries that fit one row and logs each drop.
- The rate-guard deferral cap counts deferrals in a row, not over the goal's life.
- Compaction keeps message ages (an old money receipt no longer keeps its full text) and adds no "[HISTORY CUT]"
  note when nothing was cut; a rejected call's correction note no longer stacks up.
- A reused sub-agent's new run no longer takes its opening plan as the final report.
- A mistyped tool name is no longer repaired into a different money or outbound action (`launchpad_claim` ran as
  `launchpad_sell`); the call gets the normal refusal instead.
- A goal run stopped by its spend budget is recorded as a failure; it is no longer deferred and re-run at full cost.
- When the compaction summariser fails, the owner's newest in-chat instructions stay instructions.
- `/send` sends any held token: with no spend-grade price it asks one extra step (`anyway`), owner lane only. Pinned
  stables (USDG) price at their peg; an L2 WETH falls back to mainnet ETH; the agent now sees its session's verb replies.
- `goal_create` refuses a goal whose acceptance is the goal board itself ("a goal row exists", "goal_show
  read-back"); no run can complete such a goal, and each judged failure bred more goals.
- `send_message` rewrites an `/approve_p_…` / `/reject_p_…` token the owner already decided as "(already
  approved <time> — nothing to tap)" and adds one "(Correction: …)" line; a token still queued is left alone.
- A cron job's `payload.post_spacing_hours` (`polyrob job edit --post-spacing-hours`) is now enforced by the
  X tool: a post, quote, thread or repost inside the window since that job's own last post is refused, so
  pacing is no longer prose the model checks after posting.
- A swap Confirm whose fresh quote still clears the confirmed minimum re-requests the route at a tighter
  slippage instead of refusing "the minimum received fell below the confirmed quote"; the floor never drops.
- Two first writers of a new session no longer raise "user_id cannot be determined": the session's user is recorded
  before its directory exists. The REST skill API writes a tenant `rules.json` atomically, through the skill writer.
- A token the owner pinned (`/wallet trust`) passes a buy-screen check no provider answered (USDG on robinhood
  never gets `is_honeypot`); hard failures and a high sell tax still refuse. A swap dry run now runs the screen.
- The unchecked-route refusal and the spend-limits brief now name the owner's pin verb
  (`/wallet trust <chain> <address> <SYMBOL> go`) and say no setting raises the $5 ticket.
- A tool call that restates its own tool as an `action` arg (`twitter_whoami(action='whoami')`) no longer fails
  validation; the arg is dropped only when the tool declares no `action` field.
- A scheduled run that found nothing new sends no message (the autonomous contract said "silence is a failure mode" and
  no-op cron runs began to ping the owner every 2 hours); a job text that asks for a silent close binds.
- The bundled-skill seed runs when the service can write the data home through its group (prod is `root:polyrob-data`);
  an owner-uid check refused it, so nothing was seeded. A root shell on a service-group home skips without an error.
- A one-shot cron job that ends `failed` is disabled in the same write, as `done` is; an old failed row with `enabled=1` is
  disabled when the store opens.
- The deploy deletes skills that left the shipped library: stale pre-pack copies on the box were indexed as builtins,
  so every pack skill was renamed `<pack>:<id>` and a pinned plain id served the old body.
- The `prompt_manifest` row is written after the first call, so it records the tool-schema cost and the first-step blocks (it read
  0 and missed them), with a size per foundation layer and the provider's own `prompt_tokens`.
- A cron job that finishes as `done` is disabled in the same write, and the cron store clears old done-but-enabled rows and the dead `authored_by`/`adopted_skills_digest` payload keys when it opens; finished jobs no longer read as live rails.
- The owner/self doc writer refuses a second `## Superseded` heading and an agent write that would retire more than 3 active rules at once; a misplaced heading demoted 27 live owner rules twice in one morning.
- A `/send` proposal to an address that already has an open `/send` card is refused unless it names the cards it `replaces` (closed) or says `also_owed`; re-split payouts stacked ~$233 of cards on a $100 pool.
- A typed owner reply to an open `present_choice` card closes it at once ("replied in text", never parsed into a pick); before, the turn sat until the wait ran out.
- A service-user write into a setgid shared store (skills, identity) is group-shared 0660, not 0600; before, only root writes were.
- Self/owner identity docs are written 0660 (group-shared), not mkstemp's 0600 — a promote left `owner.md`/`self.md` unreadable to the `polyrob-data` group.
- `present_choice` gets at least its declared `wait_seconds` (+30 s) from the controller; the generic 180 s action timeout cut a 300 s owner choice short.
- A goal run a rate guard refused (`retry_after_sec=N`, e.g. the X post cooldown) is deferred, not failed: back to `ready` with `payload.not_before`, no failure counted, at most 3 deferrals in a row.
- A `tool_call` the target rejects now lists every parameter (type, required, description) and names empty `arguments`; the X post-cooldown hint names `cronjob_schedule`'s real `schedule` field.
- An unblocked goal is no longer held by the dispatcher's own "Unblock goal" ask: `GoalBoard.unblock` now releases it, as retry and the outage requeue already did.
- Recall and live limits survive a tool-free step; aux calls record tokens and billed cost; `AUX_AUTO` works on OpenRouter and reflection
  runs; a live chat follows a moved model pin; the server no longer loads the code root's `AGENTS.md` as project context.
- Long owner chats: one compaction policy (the 90 % prune is gone); a prune keeps the summary and the latest owner message;
  the first chat message is dated and the newest owner message wins; owner instructions survive compaction as instructions.
- Jobs and goals keep a run record on every exit (time cap, pause, step budget) and see `<previous-runs>`; a goal reads the
  answers to its own asks; answers are consumed after a completed run; a plain owner reply reaches the job it answers.
- Memory recall ranks by recency and by the reader's regime (a job's own runs first), dates every hit, keeps `[unverified]`
  through summaries, and says "could not read" on a failed read; a reload no longer re-writes old findings as new.
- Tool results over the wire cap are saved with a real read path; children report `incomplete` and their full text; a reload
  re-renders changed rules/prompt/skills; `<live-health>` carries the live spend limits; a money denial names the caps.
- A deployed instance has one `<data home>/.env`, one env ladder and one data dir; a write `polyrob.env` pins is
  refused naming that file; `explain` labels `env:systemd|file|shell`; `/restart` is promised only when it applies.
- A guard-refused money quote makes no card and no `go` hint; the per-tx refusal names its cap and the
  `/config set` remedy; owner-verb replies and card taps (as the line confirmed) reach the owner thread.
- Money-verb results under 24 h keep full text through ageing; the step clock reads UTC; the compaction warning reads the 70/85 ladder; `cronjob_edit` says a new schedule runs a job sooner; an owner money QUESTION gets a quote and a card.
- A blocked goal put back to `ready` (owner retry, provider-outage requeue) closes the dispatcher's own "Unblock goal" ask; before, that open ask held the goal and nothing dispatched. An owner-decision ask still holds it.
- `defi_data.reconcile` compares only the chain it reads: a ledger row naming another chain (or one the rail store holds elsewhere) is skipped, a chainless row reading zero is "unscoped" (UNVERIFIED) not missing, a row marked CLOSED is closed, and an unbacked rail row names `/writeoff`.
- OpenRouter's in-flight 402 (`in_flight_budget_exceeded`: a funded account whose concurrent reservations pass the balance) backs off and retries like a rate limit; it no longer latches the credit sentinel, pauses autonomy for 6 h or fails a goal over to another provider.

### Security

- A goal or cron `target_token` is identity trust only with an explicit `target_authored_by: owner` stamp (owner seat or
  verbatim owner turn); a row with a target and no stamp, such as one written before the stamp existed, is restrict-only.
- A multitenant console lists the instance's signer approvals only for the owner tenant.
- `POLYROB_SUPPORT_URL` and `POLYROB_SUPPORT_HANDLE` have no default (the link is hidden until set); shipped code, tests
  and skills carry no instance identifier; the task-API access refusal is generic.

## [1.4.0] — 2026-10-13

**Upgrading from 1.3.x:** move posture-member lines (e.g. `SHELL_TOOLS_ENABLED=false`) into one
`POLYROB_OVERRIDES=...` line — a direct member line is now ignored; rename `BOT_INSTANCE_ID`,
`BOT_OWNER_USER_ID`, `MASTER_SEED` and `WEBGATE_MULTITENANT`, which now refuse to start; update imports
of the moved modules. A `DEFAULT_MODEL`/`CHAT_MODEL` pin now wins over `POLYROB_<PROVIDER>_MODEL` for the pinned
provider. `polyrob doctor` names each line to change; see [upgrading](docs/guide/upgrading.md).

### Added

- `polyrob service start|stop|restart|logs`, `/restart`, `/update`: finish the work, exit 75, restart, report back;
  an update that never starts rolls back. `/status` names settings awaiting a restart.
- `defi_trade.bridge` bridges INTO Solana (native SOL or USDC). The arrival is proven by the delivering transaction's
  own balance change for our account (a trade during the wait never counts as the bridge); Relay's Solana id is a registered alias.
- `defi_trade.bridge` takes the origin chain's pinned USDC as the origin (`token_in="usdc"`, `/bridge … from usdc`,
  `--token-in`): on EVM an exact approve to the pinned Relay depository, then the guarded deposit, revoked on refusal.
- `polyrob.sdk`, the stable host API for packs; packs no longer import private host names.

### Changed

- The composition root (`bootstrap`, `initialization`, `autonomy_runtime`, `bot`) moved from `core/` to `polyrob/app/`;
  `core/intel` and `core/rails` moved to `tools/`. Importers of the old paths must use the new ones (no shims).
- The agent loop's own types moved from `tools/` to `agents/task/loop_types/` (`tools.controller.types`,
  `tools.browser.views`, `tools.dom.views`, `tools.controller.registry.views`, no shims), and
  `agents.task.goals.autonomy_marker` is now `core.autonomy_marker`.
- The layering ratchet tiers cron, app, migrations, entry points, SDK and packs, counts `"pkg.mod:attr"` strings and
  caps the largest import cycle (588 → 200 modules).
- The 51 posture-bundle member flags are set only by their bundle or by one `POLYROB_OVERRIDES=FLAG=value,...` line
  (file access); a direct member line is ignored and `doctor` shows its replacement. Flags 722 → 672.
- Git, MCP stdio, lazy installs, coding snapshots, `self_env`, local `run_code` and third-party packs run beside the
  wallet in every signer mode; the key is protected by isolation. The shell floor is judged per command segment.
- One owner-turn definition, one view of the three taints, one `TurnFacts` seat-context constructor and one policy
  hook chain; HTTP `/execute` and `/lp` run the denylist, effect and refusal-taint hooks; `tx_guard` asks `authorize_spend`.
- Verb kinds (outbound, produced output, late send, read job, room-refused, background) are columns of the verb rows;
  every gate and list derives from them.
- One database registry (`core/db_manifest.DB_REGISTRY`) feeds the backup set and the shell guard; named store-path
  helpers replace open-coded `.db` joins.
- The money authorizer `tx_guard._authorize` is 25 ordered step functions over one `GuardContext`
  (`core/wallet/tx_guard_steps/`); the step order and the exact decisions are pinned by tests. No behaviour change.
- The god files are split with no behaviour change: Telegram owner verbs use one seat table, each status section
  has its own module, the defi tools use `*_verb.py` modules, GoalBoard is store mixins, and `Agent.__init__` is builders.
- `import core` no longer reconfigures the host's root logger; the process entry points set up logging.
- First-party packs inherit the core version, and the pyproject extras are the one dependency spec; a release
  bump edits `pyproject.toml` only and the lazy-install pins derive from the extras.
- Console writability and secret masking come from a per-flag `Access` column in `docs/CONFIGURATION.md`, not
  from name patterns; one "Posture bundles" table lists each bundle's members.
- The deploy reconciles every installed unit that `deployment/units.toml` declares, restores them on rollback and
  ships only `scripts/ops/`; the host env templates are generated from the flags catalog.
- Private tests and lab harnesses live under `ops/`; shipped tests read no private file, `tests/unit/scripts` is
  collected again, and one classifier (`scripts/public_export.sh`) builds and tests the public export in CI.
- The test suite runs in parallel (pytest-xdist; CI = a static ratchet job + five shards), the root conftest costs
  ~1 ms per test instead of ~7.6 ms, and the ratchets share one parse cache.
- Releases before 1.0.0 moved to `CHANGELOG-archive.md`.

### Removed

- `utils/user_utils.py`, the payment `__main__` runners, the `core.Bot`/`core.initialize_*` lazy exports and `MANAGER_METADATA`.
- The individual env rows for the 51 posture members and the `posture_flag`/`safe_local_flag` helpers.
- The unread `modules/database/schema.sql` and the dead `./.polyrob/.env` project-write code.
- The 43 flags marked "Retire after 1.4"; their default behaviour is permanent. `BOT_INSTANCE_ID`, `BOT_OWNER_USER_ID`,
  `MASTER_SEED` or `WEBGATE_MULTITENANT=true` set alone refuse to start and name the rename; `doctor` lists retired names.
- 61 internal tuning env knobs are module constants with unchanged defaults, and four dead flag rows are gone.
- The Fly kit, `deployment/legacy/`, `CLEAN_DEPLOY.md` and the dead one-off scripts.

### Fixed

- `DEFAULT_MODEL`/`CHAT_MODEL` reach every run on the pinned provider (cron, goals, planner, clients, fallback), not only
  chat; `core.runtime_config` is the one reader of the pin, and a blank `CHAT_` value no longer hides `DEFAULT_`.
- `polyrob update --apply` sees a live owner turn under the `polyrob service` user unit: the turn marker
  follows the data home, not the unit's working directory.
- Owner rules: a patch edits only the active rules (Superseded history is never rewritten), duplicate Superseded
  entries collapse, the cap is 24,000 chars, and an over-cap doc loads truncated with a warning, not dropped.
- Memory recall searches the turn's own words: a cron run its job text (not the owner-rules overlay), a goal its title
  and body, a chat turn the newest owner message; FTS drops stopwords; one injection is capped at ~9k chars.
- Memory no longer stores tool-call echoes, strategic-clue hints or short run-status lines; a one-time curator purge
  backs up memory.db, then cleans stored rows; unprotected cron/goal rows age out after 45 days.
- The X post gate says why a post needs the owner (the untrusted source read, or the agent-authored job and `/adopt`);
  a write receipt no longer taints a run, and `cronjob_list` names each job's author.
- The X DM read resolves a handle from the inbox listing's id; a handle X does not know is a dead target, skipped
  7 days; a token refresh, a late-approved send and a cron post close their X Chat client's session.
- In a scheduled or goal run, `send_message` refuses a line that starts with `@handle` and names `message()`; a
  delivered send says it went to the owner's private chat, and so does the owner-thread line.
- Unknown-setting refusals (`preferences`, `polyrob config`, `/config`, the env typo audit) find the close key even
  when its words are in another order; `preferences explain`/`set` name up to three.
- A provider credit refusal (z.ai 429 code 1113) is a named NO BALANCE error, not retried as a rate limit; failover
  starts at once, and the SDK sends it once instead of three times.
- Telegram polling backs off 1 s → 30 s with jitter on a getUpdates failure streak instead of retrying every second.
- A gated tool a session re-requests is warned once per process; the MCP tool is not offered with no MCP server.
- An owner-approved X post sent after the asking run stopped waiting, and a cron job's X delivery, now initialize the Twitter tool they build; both failed "client is not constructed" and lost the approval.
- An owner buyback of a trusted token (own launch, owner pin or owner target) on a chain no price source covers is no longer capped at the $5 unchecked-route ticket; a disagreeing route and an untrusted token still are. An EVM swap dry run now reports that refusal instead of passing.
- The X browser rail was missing from the financial-claim gate and the evidence ledger (lists named `x_post`).
- The Telegram DM gate admits `POLYROB_OWNER_TELEGRAM_ID`; a shadow signer that never answered uses the local caps
  with a notice, not $0.
- The HTTP KB, the server session tree and the owner verbs `soul`/`persona`/`knowledge`/`kb` use the deployed data home.
- Six fire-and-forget tasks, including the approval-queue owner wake, are retained; a failed `polyrob run` bootstrap
  turns logging back on.
- An Agent built without a `memory_manager` keeps its chat persona; the memory fallback no longer replaces the
  config the MessageManager reads `persona_block` from.
- `send_message` says it reaches only the session's own person (the owner in a scheduled run) and names
  `message(target=…)` for rooms, so a room reply no longer lands in the owner's private chat.
- Env reads the flag catalog missed are documented, including `BYPASS_PAYMENT_FOR_ADMINS` (default on); nineteen
  flags that read only the exact string `true` use the shared parser, and a test checks documented defaults.
- `bs4`, `yarl`, `eth-utils` and `eth-abi` are declared dependencies; a test fails on any undeclared import.
- Refusals, remedies and the profile unit no longer tell users to run `scripts/` or `deployment/` files.
- The migrations schema-version fallback names 1.10.0, the newest shipped migration, not 1.4.0.
- `VOICE_TRANSCRIPTION_ENABLED` is documented as default ON, which is what the code does; the docs and
  `polyrob doctor --flags` said OFF while every voice note was transcribed.
- The console Activity stream reads telemetry from the one telemetry location (`core.event_log.telemetry_db_path`),
  so it no longer shows an empty system feed after the sidecar move or with `TELEMETRY_EVENT_LOG_PATH` set.
- A large tool result offloaded during a run whose workspace is the project folder is written there, where
  `filesystem_read_file` looks, instead of the session directory the read could never reach.

### Security

- One owner-set security settings table (`SECURITY_SETTINGS`, `core/security/settings.py`): every check that can refuse
  natural work reads its row; the fixed floor rows are listed beside it and shown on the `custody` status line.


## [1.3.0] — 2026-10-09

### Added

- **Terminal parity:** host shell at posture 3 behind one command guard; jobs with stdin, PTY, notify and receipts;
  sudo prompt; ssh, Podman, Modal, Daytona, Vercel and Singularity backends; egress proxy; tool-calling scripts and
  a persistent Python kernel in `run_code`.
- **Full mailbox for the agent:** list, read, save attachment, threaded reply, forward, folders, mark, move and delete
  to Trash; `email_send` gains cc, bcc, html and attachments, every recipient gated. `email_read_machine_mail` returns
  activation links and codes from auto-generated mail only.
- **Room moderation:** `room_moderate` mutes, unmutes, bans, unbans and deletes messages in an allowlisted room; owner
  and admins are never targets, and a room turn or tainted session cannot call it. `room_read` lines carry `{uid=… msg=…}`.
- **Telegram post deletion:** receipts carry a message id; `message(action="posts")` lists the agent's posts and
  `message(action="delete")` removes them by row, id or last N on owner instruction (not past 48 h or others' posts).
- **`/adopt`:** the owner makes an agent-authored cron job or goal their own from an action card that shows the whole
  task, every payload key and each pinned skill with a content digest, and binds them.
- **Procedure skills:** `incident-response`, `adversarial-consensus` (blind `delegate_task` panel, judged, never
  averaged) and four `coding-workflow` references (review, triage, debugging, cleanup).
- **The agent knows the version it runs:** `<environment>`, status and `agent_status` show `get_version()` and the
  deployed sha; `agent_status(release_notes=...)` reads the shipped CHANGELOG.md.
- `defi_data.wallet_activity` on another address lists our own recorded payments to it from the wallet audit ledger
  (an unreadable ledger reads UNKNOWN).

### Changed

- **Wallet caps:** with a signer installed, every agent limit (per-tx, daily, x402 per-payment and window) clamps to
  the signer's; the signer is the one envelope, raised only by root. Each limit flag has one resolver.
- **Shell defaults:** ceiling 300 → 600 s and a longer timeout becomes a background job; a gated `shell_run` waits for
  the owner only on a dangerous command (`SHELL_APPROVAL_MODE=every` restores the old behavior).
- **One authorship rule for standing work:** owner authority reads a positive `authored_by=owner` stamp; an owner turn
  that read outside text writes agent work and says so. Owner goals carry owner cron jobs' standing authority.
- **Fewer owner taps:** trusted Solana and EVM buys, Polymarket and plain Hyperliquid cancels, sends to the owner's or
  own address, `$(…)`, relative `rm -r`, `env | grep`, workspace dev commands and 12 console settings need no approval.
- **Cron runs follow the owner:** a run opens with every newer owner rule marked as overriding the job text; an
  approved answer to a run's ask re-runs the job now, and every seat says where the answer went.
- **Rules docs hold twice as much** (owner 16000, contract 8000, self 12000 chars), and a background run's patch of an
  active rules doc lands a pending revision for `owner promote` instead of a refusal.
- `DEFAULT_TOOL_TIMEOUT_SECONDS` defaults to 180 s instead of 60 s, still below the step timeout.
- The unconfigured local data home is `~/.polyrob/data`, never `./.polyrob`; the Docker image uses `/app/.polyrob`.

### Removed
- The deprecated `/api/polymarket/*` and `/api/hyperliquid/*` mounts that 1.2.0 kept for one release; use
  `/api/packs/markets/...` (the old paths now answer 404).

### Fixed
- A bare `pip install polyrob` could not import `tools` (and refused the `discovery` pack): `openai` 3.x moved to
  `httpx2`, so `httpx` is now a direct base dependency.

- The agent's file tools read, copy, grep and edit a world-readable hard-linked file (uv/pnpm cache trees) and
  app snapshots ship it; a link to a private file, every append and every store/ledger read stay refused.
- `defi_data.wallet_holdings` (an explicit, public address) is a read-job verb again.
- A measured insufficient balance/allowance (LP builders) and a vetted Solana simulation that reverted are
  tagged preconditions and no longer taint the run; the LP node-rejected broadcast carries its kind.
- `polyrob autonomy on/off` and `config set` write the home `.env` the CLI reads; `--project` (a never-loaded
  `./.polyrob/.env`) is refused and names that file. `init`/the REPL no longer create `./.polyrob/sessions`;
  `uninstall`, profile and help texts name the real data home.
- An autonomous X post waits in-run for the owner's tap and ends inside the `twitter` action timeout; a late approval
  sends exactly the approved parameters once. An untainted owner-authored cron job posts without a per-run approval.
- X DMs work on encrypted X Chat: unsigned sender keys read as `verified: false`, and plaintext is used only for a cold
  open X Chat refuses, when the owner approval names it. An account with no tweets reads as an empty timeline.
- The agent can click and type in the browser again: the indexed element list reaches the prompt, and page extraction
  uses `aria_snapshot()` since Playwright 1.63 removed `page.accessibility`.
- `defi_data.wallet_activity` and `token_origin` work again: an explicit param model reaches an unannotated or dotted
  `params` argument as the model instead of splatted kwargs.
- `defi_data.reconcile` reuses an unchanged dust verdict (6 h), so airdrops no longer time out the money rails' gate;
  a market or precondition error no longer taints a run's public rails.
- Jupiter swaps decode every v6 route variant and the `route_v2` layouts; Solana swaps resolve lookup tables; a declared
  `max_spend_usd` asserts the value only, with the network fee charged to the caps.
- Wallet: an expired unsettled x402 payment resolves from chain state, our own bridge's arrival is not an "unmatched
  payment", and the agent-NFT send card shows the holdings.
- `hf_deploy`/`app_deploy` refuse a file that changed after the last green `run_tests`; a delegated child stopped at
  its step limit reads "stopped before finishing"; `done` turns back once while todos are open.
- A goal run whose `done()` opens with `BLOCKED:` is blocked, not done; a blocked goal has one open owner ask; goal
  claims fence each run, so a stale run cannot overwrite its replacement.
- Scheduled jobs respect live edits and cancellations; stepped day fields such as `*/2` keep their days; `cronjob_show`
  names when and via which surface a job was cancelled.
- A turn never delivers the same reply twice, and the LLM-timeout notice says the step is retrying instead of asking
  the owner to resend (which could pay twice).
- A tool's deliberate `ServiceError` refusal reaches the agent without a traceback; the log keeps it.
- A failed lookup is not proof of absence: a not-found read names the path tried and offers same-name files; a finding
  beside a failed tool call is stored `[unverified]`.
- The workspace note never invents an upload: relative paths, record-backed upload claims and declared `authored_by:`;
  A2A peer messages are labelled as not the owner, and an unreadable self-context doc is named.
- SQLite opens tolerate a sidecar unlinked by another account, and the shared data root is never sticky, so the outbox
  dispatcher no longer fails with "readonly database".
- Cancelled, failed and resumed autonomous runs release their sandbox; sandbox reapers skip when the Docker socket is
  unreachable; the cross-session turn store has its own 100k-row bound.
- Room moderation no longer repeats (room posts join the room ledger, deleted lines are marked), and Telegram polling
  logs one recovery line after an outage streak.
- `config set/unset` and `tools enable/disable` keep the home scope with `--global`; Microsoft 365
  `Authentication-Results` that open with a result are read; owner CLI API-key creation works under the strict tier.

### Security

- Deployment: ops loops run as an unprivileged account; the agent never runs as root and its HOME leaves the data home;
  root steps use pinned no-follow descriptors; rollbacks stay inside configured paths; console vhosts pin TLS and HSTS.
- Signer: its own venv; serves only a client's main process; keeps full approval details in a bounded queue; needs
  on-box approval for owner-queue verdicts; Hyperliquid RPC signs cancellations only; clients refuse before cap check.
- The spend ledger is sealed with a seed-derived MAC; browser egress closes the host's own and special-use addresses;
  `CODE_EXEC_SSH_KNOWN_HOSTS` pins the ssh backend's host key.
- CI and release: least-privilege workflows, SHA-pinned actions, digest-pinned base images, a checksummed gitleaks
  download, a read-only build job and tag-only release; PyJWT and multidict floors rise.
- Console: owner authentication on local consoles including Socket.IO; session JWT audience, strict cookies and one
  origin check; login budgets persist and group IPv6 by /64; logout is a same-origin POST; scripts are self-only.
- Console and API isolation: tenants cannot read MCP, health, telemetry or instance flags; trust and money flags are
  file-only; `polyrob dashboard` boots with a one-time password; session feeds cannot join global rooms.
- API keys carry read/write scopes and bounded expiry (legacy keys are replaced via wallet sign-in); rate limits key on
  an IPv6 /64; SIWE nonces, the A2A list page and live SSE streams per caller are bounded.
- Billing: credits reserve before inference and settle once; the compute tier gate covers every paid surface; unpriced
  models are refused; billed sessions get standard step and tool bounds; continuations recheck payment admission.
- Payments: settlement validates hash, chain and replay; test-network invoices and deposits are never income; inbound
  submissions persist before settlement; x402 recipients are bound at approval and signing.
- Spend accounting counts EVM gas, Solana fees and rent, rounds risk upward and keeps decimal deposit precision; direct
  transfers need owner approval, and payees that look like a recent payee (address poisoning) are refused.
- Swaps, bridges and LPs verify LI.FI, Jupiter and v3 calldata minimums and recipients before signing; floors compare
  against independent prices; Solana routes refuse unrelated outflows; positions size from simulated balance changes.
- Venue orders reserve at the network boundary, use live reference prices with a price band and worst-case slippage,
  enforce exposure and leverage limits, and never book a rejected order; worst market fees count against the caps.
- Untrusted content: repository, filesystem, delegated and paid-server results are framed as data; forged control
  fences are neutralized; room titles, blocked members' history and pool labels stay out of instructions.
- Provenance: model-created goals and cron jobs keep agent provenance; persistent rules and skills need owner review;
  an `owner_ask` answer must quote the owner's turn; correspondent turns cannot keep private owner documents.
- Shell guard: newline boundaries, wrappers and brace groups are judged; wildcard grants over shell syntax are refused;
  git takes plain branch names; non-Python `run_code` runs through the guard; owner-only CLI verbs refuse under the agent.
- Surfaces: non-owner DMs are denied on non-Telegram surfaces; the WhatsApp webhook secret is required; email senders
  need an MX-authenticated From; IMAP verifies certificates; voice media is bounded; pairing codes expire.
- Filesystem: SQLite stores are created private and linked stores refused; publication, export and attachment reads are
  confined; file tools use `O_NOFOLLOW` descriptors; CLI startup ignores project-local env and MCP files.
- Secrets: endpoint credentials are redacted in config listings, errors and logs; X cookies import through hidden
  prompts; provider failures omit credential-bearing URLs; legacy unguarded Twitter writes are retired.
- Packs and updates: a third-party pack builds to a wheel with no outside files; index rows admit only plain names; a
  profile review shows its env keys, MCP, cron and skill content; app deploys bind the approved directory and digest.
- Memory and identity: planner and recall writes stay scoped and new notes await owner review; correspondent sessions
  write no owner episode; ERC-8004 identity selects only a self-minted token.

## [1.2.1] — 2026-10-03

### Changed

- **Agent-NFT snapshot, inspect, journal and bind run in core:** they no longer need the agent-NFT package, which keeps
  only mint, adopt's pfp/positions/Brief and a collection's own view lines.

### Added

- **`VALUELESS_CHAIN_MONEY` (off):** the owner's opt-in to a core run on Robinhood Chain testnet 46630 — its native coin
  is priced at $0, so the USD caps do not bind, and every fee stays bounded in wei.
- **`agent_nft_adopt`:** an owner-approved verb that reads an agent NFT's journal history from the chain, runs the
  package's adopt, then writes a handover entry; a note with no action is now an on-chain journal entry too.
- **`/nft trust <collection>`:** the owner trusts an agent-NFT collection from chat — a read-only quote card, then `go`
  writes an owner pin (no mint/reveal) beside the token pins; the root file stays for admin pins. `/nft` lists the trusted collections.
- **The agent-NFT journal goes on chain:** each account action carries its signed entry in the same `executeBatch`
  (`JournalLog.log`); the next entry continues the chain read from the account's events. No site upload.

### Fixed

- **Reconcile gate timeout:** `defi_data.reconcile` prices the chain side from the batch prefetch (booked tokens
  first) and values rows on a small pool; a 116-token treasury fell from >90 s (past the 60 s budget) to ~13 s.
- **Sandbox reapers:** both skip when the process cannot open the Docker socket (the agent identity is outside
  `docker` by design), ending ~100 refused `docker ps` WARNING lines a day.
- **Identity doc caps:** owner facts rise 4000 → 8000 chars and SELF 2200 → 6000 (both had filled and refused a new
  owner rule); tool descriptions quote the live caps.
- **X login:** building an X client no longer spends the rotating refresh token, refresh logs name
  their process, and `/x login`/`/x status` say the 2-hour access token renews itself.
- **Holdings watch:** the scan runs off the event loop; the owner hears once after 6 failed passes in a row
  and once when it works again.
- **`/journey`, `/recap` and `polyrob journey` default to 24 h** on every seat, and the REPL reads the deployed home.
- **Outbound delivery:** the auto-opened breaker probes and closes; a repeated message is sent, not swallowed; a
  process drains only its own surfaces, on a lease; quiet holds keep target, files and full text in `digest.timezone`.
- **Money waits survive a cancel:** Ctrl-C during any receipt wait (send, swap, LP, claim, launch, deploy, call, NFT) records
  the broadcast against the cap and `/book` and says the tx may have gone through; Telegram runs every `go` verb off the poll loop.
- **Console reads say when they fail:** identity, memory, Work › Log, pickers and the activity tail report "could not read"
  instead of empty; the thread re-syncs after a reconnect or `feed_gap`; own_ops opens every listed chat; money cells format.
- **Seat parity:** bare `/approve` never decides; one `/kb` grammar; the REPL reads the deployed owner and instance; inline
  approvals need a slash token; console typos get the help answer; quote-card Refresh re-quotes from the typed words.
- **Chat surfaces:** Discord stops on a fatal close code and no longer blocks its gateway; IMAP runs off the loop with a
  timeout; harness replies split to the platform limit; WhatsApp/Feishu replays refused; email auto-replies ignored.
- **HTTP API:** `Bearer rob_…` keys and `X-Service-Token` authenticate; the JWT `user_id` claim wins; validation errors are 422;
  `/v1` accepts content-part arrays and streams errors and usage in the OpenAI shape; A2A push-config reads and deletes work.
- **Agent-NFT journal entries are signed under `WALLET_SIGNER=remote`:** the signer's new `journal.sign`
  op signs the account journal template and nothing else; before, no entry was ever written.

### Security

- Owner authority needs an `authored_by=owner` stamp (`python -m cron.stamp_authorship`); `/adopt` shows
  all and binds pinned skills; the shell guard resolves symlinks and state-db writes; taint exemptions
  are tagged at the source; an autonomous X post waits for the owner's tap, not "denied".
- Entry points run isolated; the data home never comes from the cwd; the shell guard sees through
  wrappers; pack wheels, profile installs, git refs, app deploys and file ops are pinned; scrubbing,
  the X lock, TLS and CI permissions are tightened.
- Deployment counters replace legacy agent-owned files atomically with private inodes,
  preserving open aliases and refusing link redirection during upgrades.

- Require an approved USD ceiling for Hyperliquid orders and show venue USD amounts on every approval card; live price or slippage changes cannot exceed the approved ceiling.

- Bound Telegram chat dispatch and voice transcription; a cancelled or timed-out transcription retains its CPU slot until the worker finishes.

- Refuse third-party code packs when remote signing is available and refuse their installation as root, preventing pack builds from sharing signer privileges.

- Enforce atomic per-tenant memory row and content-size limits, preserving existing knowledge when an oversized replacement is refused.

- Publish profile backups atomically with owner-only permissions, refuse existing output links/files and clarify the limits of text redaction.

- Detect unlabelled English BIP-39 seed phrases by checksum and Solana key arrays by their public key before persistence or export.

- Default new-wallet welcome credits to zero and refresh stale access-token holder entitlement before access; unverifiable ownership fails closed.

- Broaden address-poisoning checks, refuse unreadable history and apply the same recipient check to agent-account NFT handovers.

- Mark settled x402 payments for refund review when HTTP-200 JSON reports an application failure, including JSON-RPC errors.

- Share durable hourly X write limits across API and browser tools, workers and sessions; reserve all thread posts before sending.

- Scrub episode content and structured metadata, and use the same scrubbed knowledge text for keyword storage and vector embedding.

- Verify the expected X account before persisting an OAuth callback; bind login links and refreshed credentials to that account ID.

- Keep Signal identity stable across phone-number privacy changes by requiring the daemon account UUID; legacy phone-based permissions need review on upgrade.

- Label human transcript authors without implying owner authority and keep different senders distinct.

- Validate local dependency overlays, retain root ownership of fresh browser installations, and run deployment imports and migrations as the agent service account.

- Bind profile installs and updates to reviewed content digests and immutable remote commits; reject linked, special and oversized source files.

- Disable generated mass mentions and application links across chat transports; keep private command replies out of rooms and bound denial notices.

- Require authentication for API docs, schema and model discovery; expose detailed health metrics only on the administrator route.

- Reject lossy session identifiers instead of stripping or truncating them; API and console data paths require canonical IDs.

- Keep automatically indexed agent documents in a separate unreviewed collection; only explicit ingestion adds them to default knowledge recall.

- Enforce the autonomous tool ceiling during progressive loading and show the same restriction in catalog, search and describe results.

- Preserve brain and reasoning scrub state across streamed chunks; public rooms receive only complete, scrubbed replies.

- Confine browser downloads, frame automation alerts as quoted reports, and require PKCE with expiring, principal-bound OAuth callback state.

- Pin portal Markdown dependencies to current releases and verify their CDN bytes with SHA-384 subresource integrity.

- Drop Fly startup privileges before loading volume code, refuse root checkout installs, and stop persisting or sourcing process secrets as shell text.

- Require hashed release wheels for legacy optional installs, run the API container without root, bind Compose to localhost, and disable the retired TLS-key copying script.

- Autonomous coding-agent launchers refuse root; workspace and coding I/O reject hard-link escapes.
- Room and correspondent findings cannot enter owner memory; explicit job tools obey the current agent ceiling,
  resumed autonomous sessions retain their classification, and persona/toolset edits require owner review.
- API accounts obey administrator blocks across protected routes; tenant consoles cannot create owner-authored
  work, MCP hints cannot disable effect gates, and shared X credentials are unavailable to delegates.
- Persisted text redacts quoted credential keys, Basic authentication, URL userinfo, cookies and GitHub tokens;
  file guards cover Docker, Kubernetes, GitHub and Solana credential stores.

- **The REPL history keeps no secrets:** `/config set` of a secret key and any line with a 64-hex key are not
  recorded, the file is `0600`, and the display scrubber redacts a labelled private key.
- **An edited Telegram message never runs again:** edits are dropped before routing, so a corrected old `/send … go` or
  `/pay … go` cannot move money twice; Telegram dedup keeps 24 h and honours `retry_after`.
- **API keys cannot mint or revoke keys;** key validation is cached 60 s and revoked at once; x402 charges only a new A2A
  `message/send`, and a post-settlement failure is marked refund-due; `/api/chat/message` is payment-gated; uploads refuse symlinks.
- **Room offer cancel is compare-and-set:** a cancel can no longer overwrite an offer the settlement watcher just marked paid.
- **Console hardening:** an empty `WEBGATE_HOST` no longer binds all interfaces; own_ops refuses to boot without login
  config; refused socket joins stay rate-limited; `/logout` clears storage without inline script; no server text in `innerHTML`.
- **ERC-6909 approvals are seen:** the guard refuses an ERC-6909 grant and measures a claim-token move like an
  NFT's; the account approval scan and `agent_nft_revoke_all` cover ERC-6909 allowances and operators.
- **Agent-NFT account calls are held to the pinned collection:** the account's `token()` must be a pinned
  collection with unchanged code; no asset may go to the account of an unminted token; and nothing may move
  on the treasury side of an account call except the account paying its owner.

## [1.2.0] — 2026-10-02

### Added

- **Telegram avatar push.** `polyrob avatar push --telegram [--telegram-chat ID]` now sets the bot photo
  (`setMyProfilePhoto`, JPEG) and each named group's photo (`setChatPhoto`); hash-idempotent, falls back
  to the BotFather steps.
- **Check any wallet or token.** New read verbs `wallet_holdings`, `wallet_activity` and
  `token_origin` (Solana and EVM), `positions` (the agent's own P&L), the `/check` command, and
  cron price watches with an alert threshold.
- **One owner word per concept.** A glossary of owner words (chat, goal, scheduled job, decision,
  holdings, limit, network, abilities, permissions and more) is now checked across the console, the
  command help, Telegram and the terminal; the number of strings with an old word can only fall.
- **`/run` on Telegram and in the terminal:** list what runs in the background now, numbered, and
  pause, resume or stop ONE run (`/run pause 2`). Before, only the `polyrob session` command could
  do this. Refused inside a group.
- **Agent NFTs:** `/nft` lists the owned NFTs with their accounts, balances and open approvals;
  `/nft send <id> <to> [go]` gives one away; a watch tells the owner when one arrives or leaves.
  Sends, swaps, claims and portfolio reads take `account=` or `nft=`. Needs `AGENT_NFT_ENABLED`.
- **Agent NFTs:** the `agent_nft` tool (inspect, journal, revoke, mint, reveal, withdraw)
  behind `AGENT_NFT_ENABLED` (default off). The treasury acts through an NFT's ERC-6551
  account only as the NFT's owner; positions take `account=`.
- **NFT collections:** the owner pins one in `/etc/polyrob/agent_nft_collections.json`
  (empty by default: every mint and reveal refuses). `tx_guard` checks exact mint and reveal
  shapes; `AGENT_NFT_REVEAL_MAX_GAS_USD` (1.00) caps a reveal.
- **Cron write jobs:** `payload.write_verb` runs one allowlisted write verb (the collection
  reveal) with no model turn, through its own gates and `tx_guard`. Off unless
  `CRON_WRITE_JOBS_ENABLED=true`; only an owner-authored job may carry one.
- **Cron read jobs and pause windows:** `payload.read_verb` runs one allowlisted read verb
  with no model turn (`deliver_on` `alert`, `always` or `never`); a tick inside
  `payload.pause_windows` is skipped at $0. Read and write jobs may recur every minute.
- **Uniswap v4 liquidity:** `defi_data.pool_metrics` monitors a Pons v4 pool, `lp_pool_info`
  and `lp_quote` read one, and `defi_trade.lp_add(protocol='v4')` adds a full-range
  position. New caps `LP_ETH_CAP` (default 0), `LP_ETH_DAILY_CAP` and `LP_ETH_FLOOR`.
- **X login from chat:** `/x login` renews the agent's X API login from any owner seat
  through a one-time authorize link (needs `X_OAUTH2_REDIRECT_URI`); `/x status`, `/status`
  and `polyrob doctor` show the login state.
- **Packs:** optional extensions (`polyrob.packs` entry points, `pack.toml`) add tools,
  verbs, routes and skills; `polyrob pack list|info|doctor|enable|disable|install|remove`,
  `POLYROB_PACKS_DISABLED`.
- **Cron:** a job whose rail needs a credential only the owner can fix (an expired X login,
  an X API 402) is skipped at $0 instead of running a model turn; the owner is told once.
- **Cron:** a job that names the same owner-only remedy on 3 runs in a row raises one owner
  question: I will run it, or retire this job.
- **Owner money verbs:** `/send`, `/swap`, `/pay <url>`, `/writeoff` and `/unquarantine` on
  Telegram and the REPL. The bare form quotes; `go` acts within the quote (+5%, or `max
  <usd>`). The chain is never guessed; a token is an address. Not in rooms.
- **Action cards:** a money quote is a card with Confirm / Refresh / Cancel
  (`/card_<id>_ok|re|no`); Confirm runs the quoted line once, with every gate. `/cards`
  lists open cards; bare `/send` or `/swap` and the console's *Make a move* build the order.
- **Agent tools:** `present_choice(question, options)` shows 2–6 buttons and returns the
  owner's pick; `propose_action(command, why)` puts a money verb to the owner as a card.
  Both are refused in rooms, in sub-agents and in correspondent sessions.
- **Token trust:** `/wallet tokens`, `/wallet trust|untrust`, `polyrob wallet
  pin-token|unpin-token|pins` and console Money › Book. A buy of an unproven look-alike asks
  the owner once; a token this instance launched is trusted.
- **Buy targets:** a goal or cron job may declare `target_token: {chain, address}`; the run
  then acquires no other non-canonical token, and the work it creates inherits the target.
- **Solana sends:** `defi_trade.solana_transfer` sends SOL or an SPL token by mint, creates
  a missing token account, defaults to `dry_run`, and refuses Token-2022 transfer fees,
  hooks and unknown extensions.
- **Wallet submissions:** `polyrob wallet submissions` lists unresolved submissions (one
  blocks every send); `polyrob wallet release-submission <ref>` releases one only by booking
  its worst-case charge (`--charge-usd`, or `--never-sent --reason`).
- **Wallet caps:** `polyrob doctor`, `/status` and `polyrob wallet set-cap` say when the
  agent's cap is above the signer's hard cap in `signer.toml`, and name both remedies.
- **x402:** `x402_pay` joins the autonomous toolset when `DEFI_AGENT_AUTONOMY` is armed: up
  to `X402_AUTONOMOUS_MAX_USD` it pays and reports, above it waits for `/approve`.
  `x402_fetch` takes `request_id`, so a retry never pays twice.
- **Trading skills:** ten procedure skills (`token-identity`, `pre-trade-check`,
  `trade-execution`, `exits`, `dca` and more); `treasury-trading` is split into five smaller
  skills; `hyperliquid-trading` and `polymarket-trading` teach the basics.
- **Skills:** a skill's `requires` list pulls in at most two prerequisite skills past
  `max_skills`; a disabled skill is never pulled back in.
- **Chat:** `/why [n]` lists the last gate refusals (default 5, at most 20) with the tool
  and the gate's reason, on Telegram, the REPL and the console.
- **Chat:** `/cron list|show|add|edit|cancel` on Telegram and the REPL alike; `add` takes
  `tools=` and `target=<address> chain=<chain>`, and `edit` changes the schedule or the
  task.
- **Chat:** an owner question with lettered options (`A) … or B) …`) carries one answer
  button per option on Telegram.
- **Cron:** `cronjob_show` returns a job's full definition; `cronjob_edit` changes a live
  job in place (task, schedule, cap, rig, delivery, skills). An autonomous run may edit only
  its own jobs.
- **Outward acts:** every outward act is one `external_write` telemetry row by class
  (social, comms, public, money, code, self, network); an autonomous write in a class the
  owner paused is refused. `EXTERNAL_WRITE_TELEMETRY` and `EXTERNAL_WRITE_PAUSE_GATE` (on).
- **Security ledger:** rate-limit trips are recorded and ranked. `scripts/security_scan.py`
  runs pip-audit, gitleaks and host checks (exit 0 clean, 1 findings, 2 incomplete, 3
  error); the verdict shows on the `security` status line.
- **Memory scopes:** a goal tree or a cron job can keep its memory in its own scope
  (`AUTONOMY_MEMORY_REGIME` `shared`|`scoped`|`sealed`, default `scoped`); `polyrob owner
  memory scopes`. Behind `MEMORY_SCOPES_ENABLED` (default off).
- **Rails:** standing work is configuration: a rail is an objective with a recurrence,
  managed with `/rail` and `polyrob rails` (new, edit, on/off, grant, revoke, export,
  import). A money grant stays inert unless money is armed.
- **Workers:** `delegate_task(profile="<id>")` runs an owner-approved worker with its own
  brief, budget, model and tools; `polyrob workers` and `/workers`. The agent can only
  propose a worker. Behind `WORKERS_ENABLED` (default off).
- **Wallet signer:** `polyrob-signer` is a separate process that holds the seed, re-runs the
  money guard, signs only known shapes and holds anything above its caps in
  `/etc/polyrob/signer.toml`. `WALLET_SIGNER=local|shadow|remote` (default `local`).
- **Files:** `filesystem_append_file` takes `after_anchor` and inserts after the one line
  that contains it; no match or more than one match writes nothing.
- **Feishu / Lark surface:** set `FEISHU_APP_ID` and `FEISHU_APP_SECRET` (long connection,
  extra `[feishu]`) or `FEISHU_TRANSPORT=webhook`; `OWNER_FEISHU_ID` makes it an owner seat.
  In a group it answers only when @-mentioned.
- **DingTalk surface:** Stream Mode (no public URL, no extra), text in DMs and on an
  @-mention in groups. Credentials `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`.
- **Surfaces:** `polyrob surfaces list | add | probe` shows each surface's flag and
  credentials, stores a credential in the env file, and proves it with a read that never
  sends.
- **Surfaces:** Discord, Slack and Signal read the owner's photos and files like Telegram
  and email; a contact's files are named but never downloaded.
- **Telegram:** approval notices and `/pending` carry approve/reject buttons (a press counts
  as the presser typing the command); the "/" menu comes from the verb table.
- **Telegram voice replies:** with `/prefs set voice.replies true` the agent also answers a
  DM with a voice note (OpenAI speech or local espeak-ng). Off by default.
- **Chains:** Optimism as a read-only chain (token screens, prices, balances); no money
  moves on it.
- **Pause:** `/pause release` holds the release train.
- **Money claims:** an outbound message that says money moved ("I paid $5") is refused as
  `financial_claim_unverified` unless the wallet audit log or a settled invoice holds a
  matching record. `FINANCIAL_CLAIM_GATE_ENABLED` (default on).
- **Custody status:** a `custody` status section says whether the process is non-dumpable
  and its env scrubbed, and shows the lazy-install mode and the installed features.
- **Prompt cache:** `ANTHROPIC_CACHE_TTL` (`auto`|`5m`|`1h`, default `auto`): a 1h cache for
  an interactive turn and 5m for an autonomous run, on the Anthropic API only. `/status`
  economics shows cache `writes` (migration `v1.10.0`).
- **Knowledge:** a `knowledge` status section; the knowledge base indexes what the agent
  writes (`KB_AUTO_INGEST_ARTIFACTS`, `polyrob kb reindex`); sessions expire after
  `SESSION_RETENTION_DAYS` (90, `polyrob sessions prune`); repeated recall becomes one note.
- **Install:** `install.sh` installs POLYROB on Linux, macOS and WSL2 in one command
  (`--uninstall` keeps your data). New `polyrob setup`, `service`, `uninstall`, `config
  edit` and `tools enable|disable`; an optional extra installs on first use.

### Changed

- **Agent NFTs:** `agent_nft_withdraw_token` requires `to=`; it and `agent_nft_revoke_all` work
  without the optional agent-NFT package.
- **The owner sign-in page and its errors read in plain words** ("Sign in to Rob").
- **Live-update refusals show a plain console sentence,** never the server's text.
- **The console nav says Chat and Rob;** the chats button has a visible name ("Chats ⌘K"), the
  pause button says "Pause Rob", and the head lines are short and plain.
- **The avatar is an image slot.** Every new instance starts with the polyrob mark; set your own
  from a file, a URL or an NFT (`polyrob avatar set`, `/avatar set`, `agent_avatar set_from` on an
  owner turn). Every surface shows it.
- **Inbox cards say how long they have waited,** and when they expire if they can.
- **Rob > Overview shows each permission as a question with a Yes or No answer,** names what it
  could not check in plain words, and says where memory is kept in words.
- **The console no longer shows labels in capital letters.**
- **The console no longer shows developer source footers;** the Inbox says in one line whether
  every list answered.
- **Token reads:** prices come from DexScreener, GeckoTerminal and Jupiter and block a trade only
  when they differ by more than 10 % (25 % on thin pools); the `token_info` screen adds on-chain
  facts, Jupiter, RugCheck and Honeypot.is in a short summary. `DEFI_DATA_ENABLED` is on by default.
- **Replies:** the agent speaks to its user only with `send_message`; `done(text)` is the
  run record. A cron `deliver` gets the run's last `send_message` (none sent, none
  delivered); `style.verbosity=detailed` appends the record for the owner.
- **Prompts and tool text:** the model notes, the sub-agent contract, the output judge and
  the tool descriptions match the code: `send_message` then `done`, `propose_action` for a
  money move, `present_choice` for a choice, and the real units and limits.
- **Persona:** a chat seat renders the persona in `PERSONALITY_DEFAULT_CHARACTER`.
- **Skills:** the money, markets, X, discovery and general skills match the code and name
  chat verbs, never a `polyrob …` command; money triggers no longer fire on common words,
  and `secret-handling`, `person-analyzer` and others have narrower triggers.
- **First-party packs:** `discovery`, `markets` and `x` ship inside the `polyrob` wheel. ⚠️
  Their SDKs stay extras (`polyrob[twitter]`, `[anysite]`, `[crypto]`); without one, the
  pack's tools are withheld with the remedy. `install.sh --packs` adds them.
- **Markets pack:** the Polymarket and Hyperliquid tools are the `markets` pack; their
  routes moved to `/api/packs/markets/polymarket/*` and `/api/packs/markets/hyperliquid/*`.
- **Owner authority:** in a genuine owner turn (chat, or `/send … go`, `/bridge … go`,
  `/pay`) the autonomy pause and the autonomous ceiling do not apply; the caps and the
  simulation bind everyone. An owner approval now sends, not re-queues.
- **Refusal taint:** after a money action fails or a gate refuses it, the rest of that run
  cannot post to a public or non-owner destination; a public cron `deliver` goes to the
  owner instead. A genuine owner turn clears the taint.
- **Token identity:** `defi_trade.swap`, `solana_swap` and `launchpad_buy` check WHICH
  contract they buy before any quote: a symbol pinned to another address, a second contract
  for a tracked symbol and an unverified token above $5 are refused.
- **Book:** `/book` and `defi_data.reconcile` warn when one symbol has more than one
  contract, show positions the ledger does not list and each position's status, and no
  longer claim trading is halted on a disagreement.
- **Copy:** owner-facing refusals name chat verbs (`/resume`, `/pending`), a daily-cap
  refusal says when the cap frees, and the agent may not claim an act is queued or sent
  without a tool result.
- **Telegram:** `/config set` shows where a flag write applies.
- **Lean base install:** `pip install polyrob` carries 24 base dependencies (was 36) and one
  provider SDK; the rest moved to extras (`gemini`, `anthropic`, `memory-vector`, `docs`,
  `media`, `server`). ⚠️ Without `server` an upload is refused with 503.
- **Goals:** `/goal cancel <id>` means never again (`--once` stops only this run; `/goal
  allow` turns it back on). `/goals` shows what is off, who asked for each goal, and a `NOW
  · STUCK · NEXT · OVER` summary.
- **Config:** `polyrob config set`, the REPL `/config set` (now with `--global`) and the
  console validate and report the same way; a REPL write of a live-safe autonomy flag
  applies at once, and `/autonomy on|off` needs no restart.
- **Owner rules:** `OWNER_RULES_IMMEDIATE` defaults on (a rule stated in chat binds on that
  turn), `OWNER_DOC_MAX_CHARS` goes 1600 → 4000, a replaced rule moves under `##
  Superseded`, and a `rules` status section names anything not loaded.
- **Records:** a workspace `.md` declares `kind: instruction | record`, and the file verbs
  refuse to rewrite or delete a record (`DOC_KIND_ENFORCED`, default on). A cron job or goal
  can pin its skills (`skills=[…]`).
- **Surfaces:** `polyrob gateway` starts Discord, Slack, Signal, WhatsApp and every new
  surface once its credentials are set (`*_SURFACE_ENABLED=false` keeps one off); new
  options `--skip`, `--no-autonomy`, `--idle-when-empty`, `--host`.
- **Webhooks:** a webhook surface can answer a platform challenge and ack before the turn
  runs (replayed once after a restart); bodies are capped (64 KB, WhatsApp 3 MB) and a bot
  is capped at 20 lines a minute.
- **Money regime:** the posture card shows `supervised`, `autonomous` or `armed` and names
  what is missing; `AUTONOMOUS_RIG_DEFAULT=money_rail` no longer gives `defi_trade` to
  agent-authored work outside `armed`.
- **Prices:** `token_info` names the pool behind a price and grades confidence on that
  pool's depth; the busiest pool by 24h volume is picked; an unknown price stays unknown,
  never `0.0`.
- **Prompt cache:** `TOOL_SCHEMA_STABLE_ORDER`, `STABLE_AUTONOMOUS_TOOLSET`,
  `TOOL_RESULT_MAX_TOKENS` (6000) and `OPENROUTER_PROMPT_CACHE` default on; a restart
  replays the rendered prompt (`FOUNDATION_REPLAY`) and catalog changes arrive at the tail.
- **Late tools:** a tool loaded mid-session grows `tools[]` (default), is deferred on an
  Anthropic model that supports it (`ANTHROPIC_DEFERRED_TOOLS`, on), or goes through the
  `tool_call` bridge (`TOOL_SCHEMAS_FROZEN`, off).
- **Preferences:** `voice.transcript_echo`, `stream.telegram` and
  `delivery.lifecycle_daily_cap` are owner preferences.
- **Context:** a tool result reaches the model once; compaction thresholds are 70/85/95 %
  (`COMPACTION_*_PCT`), old long tool results shrink to a pointer first, and old images
  retire in batches. `/context` reads the provider's own token count.
- **Context:** the project-context file cap follows the model window (4%, 4,000–40,000
  tokens); `MAX_EXTRACTED_CONTENT_SIZE` drops to 100,000 characters and
  `MAX_EXTRACTED_CONTENT_TURN_SIZE` (200,000) bounds a turn.
- **Billing:** on OpenRouter the billed `usage.cost` outranks the catalog estimate; an
  absent cost is `None`, never `0.0`.
- **Update:** `polyrob update --apply` reinstalls the extras you have under
  `requirements.lock` and waits up to `--wait-idle` (1800 s) for running work (`--no-wait`,
  `--force`).
- **Models:** an unknown `claude*` id resolves by family (`opus`, `sonnet`, `fable`,
  `haiku`, bare `claude`) instead of a 200K sonnet row.

### Deprecated

- The venue API paths `/api/polymarket/*` and `/api/hyperliquid/*` still answer for one
  release with a `Deprecation` header. Move clients to `/api/packs/markets/...`; 1.3.0
  removes the old paths.
- `LAZY_DEPS_ENABLED` is a deprecated alias of `LAZY_DEPS_MODE` (`true` = `trusted`, `false`
  = `off`).

### Removed

- **The Mindprint avatar generator.** `polyrob pfp` (generate, randomize, keep, say, studio), the
  console's `/pfp.*` routes and the live terminal face are gone. A kept face is not carried over:
  the slot shows the polyrob mark until you run `polyrob avatar set <image>`.
- **Dead prompt code:** the unused system-prompt managers and the shipped
  `data/prompts/system_prompts.json` and `autov2_prompts.json`;
  `send_message.timeout_seconds` left the schema (a call that passes it still works).
- **Separate first-party pack distributions,** `PROD_PACKS` and the per-pack closure files.
  `install.sh`, `polyrob update` and `python -m migrations.migrate upgrade` retire a
  leftover one.
- **Plain chat text is never a command.** A sentence that starts with stop, pause or resume
  is chat; use `/pause` (`/halt`) and `/resume`. The plain-word approve/reject and the
  `pause.phrases` preference are gone; use `/approve`, `/reject` or the tap tokens.
- The `VOICE_TRANSCRIPT_ECHO`, `TELEGRAM_INCREMENTAL_STREAM` and
  `USER_DELIVERY_LIFECYCLE_DAILY_CAP` env flags (now owner preferences).
- The agent-writable `project/.pylibs` lazy-install directory.
- The unused `CompactionManager` policy and the dead `AUTO_KNOWLEDGE_RETENTION_DAYS`
  setting.

### Fixed

- **Honest avatar replies.** `agent_avatar set_from` and Telegram `/avatar set` no longer claim the new
  image is on every surface; they say the X/Telegram profile photos change only on `polyrob avatar push`.
- **`/x status` says what each X credential unlocks and how to restore it**: the API login
  covers DMs (`/x login`); the browser session covers posts and outreach (`polyrob x-account capture-session`).
- **`polyrob dashboard` no longer creates `./data/task` in the folder it runs from:** with no
  `DATA_ROOT`/`POLYROB_DATA_DIR`, its session tree is `<data home>/sessions`, the tree `polyrob`
  writes. `HEAD /avatar.png` answers 200 instead of 405.
- **A console run that ended without a closing event no longer reads "Working for …" forever:** it
  says "Stopped after … without a final word.", and the Timeline counts steps, not "actions".
- **`defi_data.positions` lists holdings open only in the position ledger** (bought before the rail
  book) as LEDGER-ONLY with basis unknown, read-only; an unreadable ledger says "could not read".
- **The position-ledger reader keeps the first row per address**, so superseded rows left in the
  `## Open positions` table no longer count twice or show up as reconcile mismatches.
- **Anthropic and Anthropic-compatible providers work again with `anthropic` 1.6:** a call no longer
  fails with "unexpected keyword argument 'temperature'"; a third-party endpoint still receives the
  sampling settings.
- **`launchpad_claim` labels its dollar line as the claim's gas fee** (`cost:`), not as the value
  of what is claimed.
- **`polyrob wallet init` with no `--data-dir` writes the derivation scheme under `<home>/wallet`,**
  where the running agent reads it; before, a bip44 wallet could resolve to a different address.
- **An operator-pinned Solana payment mint keeps its case;** before, the asset store lowercased it
  and named an account that does not exist.
- **An unknown console address shows the error page instead of raw JSON;** it says what happened in
  plain words and keeps the details behind "Show details".
- **`cronjob_show` names the job's provider pin** — `provider: (owner's seat)` or `<pin> (pinned)`,
  and for a credit-dead pin, the seat it reroutes to (or that runs are skipped).
- **The console no longer shows a stuck rate-limit banner after a few page loads;** live updates
  resume by themselves.
- **A credit top-up now unlatches the provider on its own.** At most once per 5 minutes the goal
  dispatcher re-checks a latched seat's balance (OpenRouter `/credits`) and releases the seat only
  when the provider reports a positive balance.
- **The chat page no longer clips the file pane or covers it with the message box;** on a phone the
  pane opens as a drawer ("Files and steps").
- **A chat's Files list shows only the files that chat wrote,** plus what you gave it; the
  shared project folder is behind one link with its size.
- **The chat's file list reads at most 500 entries four folders deep** and no longer re-reads the
  folder every 30 seconds while live updates work.
- **Starting a task where no language model is available now fails with a clear reason** (503
  `no_model`) instead of an empty chat; the console keeps your text and names Telegram and the terminal.
- **A chat shows your question even when the run failed;** a scheduled job's run says it is not a
  chat, with its task behind a disclosure. A bound chat's tab says Chat, not New.
- **The chats list names who started each chat,** tells a failed chat from a stopped one, shows
  finished chats in a neutral colour and shows times in your time zone.
- **The chats list no longer shows raw file errors.** A row with a part that would not read says
  "Part of this chat is missing."; the rows say "Done", "Unknown", "Untitled chat" and "Watch only".
- **Reopening a long chat shows its newest events,** not its oldest.
- **The chat no longer draws a step twice** or misses one that happened while the page loaded.
- **Messages you add while Rob works now show in the chat.**
- **A chat that failed now says so;** a chat waiting for your answer no longer shows as stopped.
- **The chat no longer lists sending a reply or finishing as steps,** draws each reply under its
  own steps, and ends each turn with its own summary.
- **Chat step times and the turn duration show real times** instead of 0:00 and 0s, and a
  receipt says "1 step", not "1 actions".
- **On a phone, the Schedule table labels its first column** ("What it does") again.
- **The Work pane names a partial file read** instead of drawing a blank line.
- Token reads: Arbitrum and Polygon had no price, Solana holdings lost decimals and Token-2022,
  Alchemy read one page, `reconcile` never said CLEAN on an airdropped wallet, and an unknown
  cost basis was stored as $0.
- Every tool call now records its completion. Before, the console, the REPL fallback and
  Telegram progress never saw a tool finish, because the completion was written only on a code
  path that never ran.
- Session feed folders no longer fill with lock files.
- A reply could fail to appear live in the console until a reload.
- Events of a session resumed after a restart no longer overwrite its first events.
- Rob no longer stores the conversation context it is given as a memory of its own.
- **ERC-8004 identity:** `read_agent_id` works on a registry that is not ERC721Enumerable
  (the Robinhood Chain registries), so the double-registration check no longer says "could
  not check"; agentId 0 is a real id.
- **Money figures:** `defi_data.portfolio` values canonical and pinned tokens first, so the
  wallet's own position no longer shows as raw units; `launchpad_claim` prints what a claim
  is worth. The agent gives the owner only tool-printed money figures.
- **Stall reports:** each stall of the goal pipeline names itself (`stall #N` and the time),
  so a later stall is no longer dropped as a duplicate.
- **Prompts:** the per-step reminder, the correspondent DM framing, the browser and
  Polymarket blocks and `<owner-instructions>` name only what the session really has; the
  background reviewer now sees the work it reviews.
- **Browser:** `search_google` URL-encodes the query; a `&`, `#` or `%` no longer cuts it.
- **Apps:** the approve hints name `/apps approve` and the console, not a shell command.
- **Tools:** `session_search(collection=…)` says so when the knowledge base is off or
  failing.
- **Skills:** a cron-only session no longer gets `dca` or `exits`, matched skills are
  delivered first and can be loaded again after compaction, and one malformed user rule no
  longer drops every skill.
- **Runtime nudges:** the planning, empty-action, loop and verify-before-done nudges enter
  history as control messages, not as a user turn, so a nudge can no longer become the
  active task; the texts name only registered actions.
- **X OAuth 2.0:** a refused refresh token records a re-login verdict naming `/x login` and
  is not re-sent; two processes no longer spend the same refresh token; DMs fall back to the
  OAuth 1.0a keys on a 401.
- **X posts:** a new tweet that is not readable yet is reported as X lag, not as a failed
  post.
- **`polyrob x-account`:** on a deployed box it acts on the deployed data home and refuses
  when a key it needs is missing; before, it wrote a record the agent could not read.
- **Deploy:** `RESTART_ONLY=1` restarts every unit that reads `/etc/polyrob/polyrob.env`.
- **Approvals:** a decided Telegram button is removed; the REPL and the console chat box run
  the tap tokens (`/approve_p_<hex>`, `/fulfill_<ask>_<letter>`); the fallback grant card
  carries one-tap tokens.
- **Money:** a send whose outcome is unknown says "outcome unknown — do not send again",
  never "NOT sent"; such a bridge stays `in_flight`.
- **Packs:** a pack's money verbs can register, pack faults are isolated per pack, and
  `polyrob update` installs the `markets` pack for a `crypto` install.
- **Wallet:** `defi_trade.transfer` sends the native gas asset (`token='native'`); a repeat
  `/send` to a Solana address is no longer a replay; a run that can never sign no longer
  raises an approval.
- **Wallet:** an unreadable pin or position store reads as UNREADABLE, not empty; chain
  names are case-insensitive; `approve_token` clamps a near-balance amount; armed goal runs
  can transfer, not only swap.
- **x402:** a payment above $1 is no longer refused by the SDK's default; a multi-network
  challenge is paid on Base USDC; a metered URL is payable more than once; the replay key is
  stored whole before signing.
- **Chat:** a cron job created from chat gets `CRON_DEFAULT_MAX_DURATION_SEC`; "resume",
  "continue" or "restart" never pause.
- **Coding:** the verify-before-done and ship-what-was-tested gates fire again and no longer
  fire on prose edits or a refused `run_tests`; `run_tests` writes its full output to a log
  and gets 300 s.
- **Coding:** edits keep CRLF, BOM, mode and hard links, refuse binary and read-only files
  and write atomically; `apply_patch` matches hunks more reliably; TypeScript checks use the
  nearest `tsconfig.json`.
- **Telegram:** `/bridge … go` above the autonomous ceiling no longer freezes the bot; it
  reports when it lands.
- **Publishing:** `publish` accepts a directory such as `dist/`, swaps in a complete new
  version of a live page and reports a failed promotion.
- **Hyperliquid:** agent approval, leverage changes and order cancellations enter the owner
  approval queue in both payment modes.
- **Skills:** skill provenance is per tenant; chained `patch_skill` edits make one pending
  item; a queued owner rule offers a tappable `/approve_p_<hex>`.
- **Autonomy:** rails keep a money-free base, seeders recheck due state, direct cron room
  delivery respects the pause, and a failed database read is unreadable, not empty.
- **Console:** the wallet views show the submission journal again, and Money's Cash and
  Limits say when the ledger could not be read instead of showing zeros.
- **X:** the startup self-test uses the credential the client really carries (no false `403
  Forbidden`).
- **Owner thread:** records the true surface and refuses a cron `deliver_target` that is not
  the owner's own address.
- **Webhooks:** failed turns are kept and pending events retry on redelivery.
- **Runtime:** admin token checks and billing reconciliation work again; X cooldown
  telemetry no longer raises `NameError`; speech converters get no credentials and are
  stopped on timeout.
- **MCP:** a cached MCP resource read no longer fails.
- **Uninstall:** `polyrob uninstall --purge` names and deletes both homes behind a typed
  confirmation; `polyrob doctor` reads system units too.
- **Install:** `requirements.lock` serves Python 3.11 again; a missing extra is named
  instead of a raw `ModuleNotFoundError`; `polyrob update` no longer reads a `pid: 0` marker
  as a live turn.
- **Context:** the emergency prune keeps at least the last 3 messages with complete tool
  pairs.
- **Console Inbox:** a `tool_approval` card shows the full grant card, not a 160-character
  preview.
- **REPL approvals:** `[s]ession` and `[a]lways` no longer widen a money action to every
  later amount; a money action offers only once, deny or never.
- **Console:** `/send … go` or `/bridge … go` in the chat box answers "started" at once; the
  result arrives as a notice.
- **Cron:** a job is no longer skipped as an X DM job because its task text quotes or
  forbids a DM.

### Security

- **Feishu/Lark webhooks are signed or refused:** an event outside a ±5-minute timestamp
  window is refused, and token-only mode (no `FEISHU_ENCRYPT_KEY`) refuses every event unless
  `FEISHU_WEBHOOK_ALLOW_UNSIGNED=true`.
- **A cross-chain bridge from an EVM chain pays only Relay's pinned deposit contracts:** a quote
  naming any other `to` refuses before signing (the Solana origin already pinned the program).
- **`polyrob-signer` spends an owner's over-cap grant when a send may have left**, so one approval
  cannot carry a second over-cap send after an unknown outcome or a failed ledger write.
- **An installed skill's reference files get the same Unicode and bidi-override scan as its
  `SKILL.md`.**
- **Agent NFTs:** a pinned collection's token leaves a wallet only when its account has no open
  approval, and `tx_guard` re-checks the collection's `runtime_sha256` on every mint, reveal
  or move; an unreadable or changed check refuses.
- **Token-bound accounts:** `tx_guard` refuses the Tokenbound forwarder, an account selector
  at any calldata offset (including `executeNested`), and any move of an NFT into an account
  of its own collection; only one exact approve, call, reset `executeBatch` passes.
- **NFT sale safety:** the open-approval scan covers ERC-20, ERC-721 and Permit2 grants of
  an NFT's account, with guarded ERC-721 and Permit2 revoke shapes. A read-only
  `robinhood-testnet` chain (`DEFI_EVM_RPC_ROBINHOOD_TESTNET`) serves the account reads.
- **Money and outbound rails:** launchpad `slippage_bps` is capped at 1000; `email_send` has
  the same forged-turn gate as `message`; a leaf sub-agent cannot ask the owner; the markets
  pack's own-account reads are blocked for correspondents.
- **Skills:** a user skill can never take a builtin or pack skill id (before,
  `secret-handling` could be replaced).
- **Correspondent sessions:** memory recall, the digests, the continuity bridges and the
  health note are skipped in a session whose output goes to a third party.
- **Untrusted framing:** browser, MCP and web results, recalled summaries, delegated parent
  context and attached text files are wrapped as untrusted data on every path.
- **Packs:** a manifest cannot grant itself first-party privileges; third-party code packs
  are refused while the process holds the wallet seed.
- **Money:** only the owner's own approval lifts the autonomous ceiling; a dapp page's
  transaction is never an owner turn; a failed pause probe or ledger result refuses.
- **Wallet:** the agent's file tools cannot write the owner pin and token-provenance stores.
- **Publishing and coding:** published pages carry the vhost's security headers (re-run
  `scripts/setup_publish_vhost.sh`); `coding.grep` applies the read policy to every file.
- **Custody:** the custody process is non-dumpable and the seed variables leave
  `os.environ`, so a child cannot read them; a mistyped seed never reaches a log; the wallet
  audit files are `0600`. ⚠️ No core dumps and no `py-spy`/`gdb` attach.
- **Installs:** every install is hash-pinned by `requirements.lock`. Lazy installs go
  through one trusted installer (`LAZY_DEPS_MODE` `off|trusted|legacy`, default `trusted`).
  ⚠️ A server without custody now installs lazily; `LAZY_DEPS_MODE=off` keeps the refusal.
- **Approvals:** an approve counts as an exit only to a pinned route spender; the approval
  card shows every money field and a calldata digest.
- **Swaps:** every EVM swap asserts its minimum output; `solana_swap` checks the simulated
  output and refuses an unobserved output mint.
- **Solana guard:** every top-level token and system instruction is decoded (authority
  changes, closes, durable nonces and a fee above 0.01 SOL refuse), and the Solana bridge
  requires the pinned Relay program.
- **Generic call and dapp:** position managers, v4 pool managers, Permit2 and Pons contracts
  are reachable only through their typed verbs; Permit2 grants are refused.
- **Deploy:** `deploy_token` refuses `vanity` / `salt` and must mint exactly the supply to
  the wallet; the ERC-8004 agentId comes from the receipt and a second registration in
  flight is refused.
- **Payments:** the deposit monitor credits only the increase; every EVM invoice gets a
  random sub-cent tail; a canceled subscription is never reactivated; an x402 authorization
  over 600 s is refused.
- **Gates:** wallet reads are owner-scoped; Hyperliquid and Polymarket bind the tenant per
  call; `/pause trading` binds every entry verb; a forwarded Telegram message taints the
  turn.
- **Runtime:** money verbs run off the event loop; a reverted swap writes no position; LP
  add and remove check the pool price and cap slippage at 1000 bps; the bridge re-authorizes
  after an owner wait.

## [1.1.0] — 2026-09-21

### Fixed (interface deep audit — 2026-09-21)
Five parallel audits of the console, the HTTP/A2A/OpenAI-compat API, the CLI, the
REPL, Telegram/email and cross-surface parity found ~270 defects; all are closed in
this pass. The behaviour-visible ones:
- **Console** — `GET /api/webgate/pending` renders the same composer as the Inbox
  (a locked queue is NAMED, never a shorter list); Work › Now reads `live.goals`
  (un-windowed) and dedupes against `session_id`; an ask can be ANSWERED in writing
  (`{"answer"}` rides through `GoalBoard.decide_ask(answer=)` into the retry prompt);
  a Pause/Resume control in the shell header; app Kill/Logs, file links, the chat
  file picker, Keep for the avatar; `command_reply` rendered for a slash verb (and a
  verb typed on the cold open no longer becomes a session task); knowledge/worklog/
  artifacts/invoices/live readers carry typed error/partial/`filtered_out`/
  `outstanding_*` fields; the `/pending` and `/profile` pages, `/api/sessions`
  (a 1,790-dir synchronous walk), `/api/session/{id}/stats`, the CDN script tags,
  and 19 dead assets are gone; `docs/guide/console.md` and the README match the tree.
- **API** — ONE public-path allow-list shared by both auth gates (`api/auth_constants.py`;
  SIWE, the agent card, x402 challenge/pay and pricing were 401 whenever `API_SECRET`
  was set, and a settled x402 payment then got a 401 with no refund); x402 settles
  ONLY on the gated routes; `rob_xxx` API keys validate without `ENABLE_AUTH`;
  `API_AUTH_TOKEN` is a `service` role on `X-Service-Token` (not admin; `X-API-KEY`
  works one more release with a WARN) and `GET /api/test-auth` is deleted;
  `/api/task/metrics` is admin-only; `/health` can be degraded; A2A artifacts have a
  real tenant-scoped `GET …/workspace/{path}`, `initializing` is WORKING, push URLs
  pass the SSRF validator, every session op routes through `guard_remote`, `cancel`
  checks ownership; `polyrob serve --workers N>1` refuses unless
  `API_AUTONOMY_RUNTIME=false`; `/v1/chat/completions` threads `temperature`,
  400s (OpenAI-shaped) on unsupported params, marks `usage.estimated`; ERC-8004
  `trustMode: onchain` needs a real agent_id + registry; docs corrected.
- **CLI** — every owner/admin verb reads the deployed data home
  (`admin_data_dir`) and the owner tenant (`keys`, `wallet init|book|bridges|overview`,
  `owner invoices|settle|sub|correspondents|approve`, `approvals`, `apps`, `cron`,
  `session`, `subagents`, `finance` were on a shell/CWD home — a confident zero over a
  live store); `goals list|tree` read `list_recent`/`status_counts`; `surface list`
  reads the real dead-target store; EIP-55 kept on `wallet asset add`; `doctor --json`
  is the typed snapshot; `wallet set-cap` writes the live pref; new verbs
  `wallet claim|nft|dapp`, `identity register|set-uri`, `owner correspondents --history`,
  `owner paid enable|disable|price|asset|offers`, `owner groups admins`.
- **REPL** — addresses/paths/tx hashes are no longer redacted; `/approve <id>`
  DECIDES on every seat (`/gates` manages the gates); `/reject` reads the union;
  `/cron list|add|cancel`; `/groups /mute /unmute /ban /unban /paid /cancel /new
  /start /claim /nft /dapp /contacts /identity`; `/apps <verb>`; `/status` is the
  snapshot and `/meter` the token meter; full session ids; one empty grammar.
- **Telegram** — help is RENDERED from `core.verbs` (one SSOT, contract-tested);
  a room admin can no longer self-grant `admin`; `blocked` fails CLOSED; a paired
  sender on a forgeable surface (email) is never OWNER; free `/mute /ban` no longer
  raise; `/bridge /launch /deploy /lp` no longer block the polling loop; `/goal <id>`
  resolves without `board.list`; `/wallet balances` shows the LIVE caps and the
  cap note names `/config set budget.wallet_per_tx_usd`; `/paid cancel` is
  room-scoped; `/missed` is unclipped; a correspondent's attachment is named;
  `/cron /goal /fulfill` refused from a room; room toolset is a closed allowlist.
- **Email + delivery rails** — see the D-section of the review (BODY.PEEK, dedup
  after dispatch, honest queue outcomes, pause gate on `email_send`, secret scrub,
  IMAP verdicts, attachments on AgentMail, HTML-only bodies, thread anchors).
- **Parity contract** — `tests/unit/test_surface_parity.py` scans every console
  router, carries 27 new rows (six of them all-`None` on purpose), and gains
  `test_repl_covers_every_telegram_owner_verb` + `test_board_view_never_calls_list`
  + `test_repl_help_group_matches_core_verbs`.
- **Revalidation pass (same day)** — four fresh reviewers re-read the eight commits and
  their dependents; ~45 more defects closed. The ones that mattered: the workers>1 refusal
  lived only in the CLI (the systemd entry `main.py` skipped it — now in `server_boot`);
  `require_service`'s 503 was unreachable (the container accessor RAISES, so `/api/hyperliquid|
  mcp|polymarket|admin` and SIWE answered 500); `X-Service-Token` could not pass CORS
  preflight and the console still proxied the operator token under `X-API-KEY`; the `/v1`
  OpenAI envelope skipped middleware-level 401/413; `/health` could 500 on a non-numeric
  metric; the agent-callable `read_emails` still used `RFC822` (which set `\Seen` and
  emptied the surface's inbound queue); the new outbound-queue status line looked for
  `outbox.db` on the wrong axis; a queued cross-process delivery was journalled `failed`;
  the AgentMail outage verdict could never clear; `/help gates` said "unknown command"; a
  room admin's `blocked` grant and the paired-email rule re-proven; `/fulfill <id> [answer]`
  and `/approve <id> <answer>` carry the owner's words on every seat; every CLI money ACTION
  spent under the SHELL's tenant while every VIEW read the deployed one; a `/dapp` READ
  created `dapp_sessions.db`; `KV_RE` never claimed `private_key`/`mnemonic`/`seed_phrase`/
  `passphrase`/`keystore` (they survived every scrubber verbatim); `refund_due` (a settled
  machine payment we owe back) now has one status vocabulary (`modules/x402/invoice_status.py`),
  a ledger line, a status WARN, a critical-lane owner notice and a listing filter on every seat;
  paid-room remedies are seat-aware; six REPL help groups matched to `core.verbs`; the
  `GROUP_TURN_TOOLS`/`INBOUND_MEDIA_ENABLED`/`API_AUTH_TOKEN` flag rows corrected (+regen).


### Added (057 — runtime structural upgrade, implemented 2026-09-20)
All flag-gated and default-inert unless noted; prod arms them per the proposal's
§4 defaults. Full flag table in `docs/CONFIGURATION.md` (24 new rows).
- **WS-A tool rigs** — `core/config_policy/rigs.py` (the ONE table `money_rail` /
  `social` / `research` / `ops` / `full`, fixed id order); `payload.rig` on cron jobs
  and goals, `polyrob cron edit --rig`, `AUTONOMOUS_RIG_DEFAULT`;
  `AUTONOMOUS_TOOL_DISCLOSURE` lets an autonomous session widen a narrow rig;
  `STABLE_AUTONOMOUS_TOOLSET` + `TOOL_SCHEMA_STABLE_ORDER` keep the cached prompt
  prefix byte-stable; the email tool's catalog status reads `gated:credentials-rejected`.
- **WS-B budgets by session class** — `agents/task/session_class.py` is the ONE
  answer to "is this session autonomous"; `AUTONOMOUS_CONTEXT_BUDGET_TOKENS` makes
  compaction reachable; `TOOL_RESULT_MAX_TOKENS`, `AUTONOMOUS_READ_PAGE_LINES`,
  `AUTONOMOUS_MAX_OUTPUT_TOKENS`, `LLM_OUTPUT_TRUNCATION_RETRY` (+ the
  `llm_output_truncated` event), `LLM_TIMEOUT_BY_OUTPUT` (timeout sized by expected
  output, not input), `LLM_TIMEOUT_REROUTE`, `AUTONOMOUS_SCREENSHOT_ON_BROWSER_ONLY`;
  `write_file`/`append_file` results carry `size_bytes` + `sha256`.
- **WS-C pre-empt without discarding** — `payload.preempts` (default = money) is the
  pre-emption property; the yield is scoped to the goal holding the workspace (the
  planner is never cancelled), attaches artefacts before the cancel unwinds, and is
  a `goal_run yielded` event; `GOAL_YIELD_GRACE_SEC`, `GOAL_RESUME_SAME_SESSION`
  (+ `goal_run resumed`), `GOAL_PREFLIGHT_ENABLED`/`_STEP_SEC` (056 WS5 2b),
  `GOAL_YIELD_AGEING`, `CRON_DEFAULT_MAX_DURATION_SEC`; deferrals are `goal_run
  deferred` events; the yield probe runs inside the TickLock; the change gate never
  gates a money job; `polyrob cron prune`, `cron list` hides cancelled; the goal
  dispatcher reads the cross-process turn marker.
- **WS-D provenance** — `source=`/`observed_at=` on `owner_doc_manage` /
  `self_context_manage` (changed lines stamped ` [from: <source> <date>]`),
  `DOC_CLAIM_PROVENANCE_REQUIRED` (a FORMAT guard on claim-shaped lines), the
  foundation block states the docs' age, and a room's allowlist note renders as a
  dated LABEL, never its name (`core/surfaces/room_label.py`; `group_allowlist.updated_at`).
- **WS-E verification** — `core/rails/verification.py` is the ONE per-rail proof table
  (x_post / x_reply / telegram_channel / telegram_group / email / onchain), rendered
  on the `message`, `x_post`/`x_reply` and `email_send` results, cited by
  `room_read`, generated into `docs/guide/rails-verification.md`; the corrupted
  bullet in the `x-engagement` skill is repaired.
- **WS-F durable rail verdicts** — `core/credential_verdicts.py` is a store
  (`verdicts.db`, `first_seen`/`last_seen`/`count`, shared by every unit); the email
  surface polls IMAP without the SMTP probe; the X API 402 is a durable
  `x_api_rejected` event; a missing key is a verdict warned once per process.
- **WS-G identities** — `polyrob doctor --perms [--strict]` over `core/data_perms.py`;
  the deploy's ownership pass covers the whole data home; the three ops-loop units
  join `polyrob-data` with `UMask=0002`; a root euid refuses owner-CLI writes on a
  deployed box (`--as-root` / `POLYROB_ALLOW_ROOT_CLI`).
- **WS-H deploy + shutdown** — `deploy_when_idle.sh` fails closed, parses the marker
  as JSON and carries a daily budget (`DEPLOY_DAILY_MAX`, 6); console/API turns hold
  the turn gate (`SURFACE_OWNER_TURN`, ON); on SIGTERM the in-flight cron/goal run
  gets an ending (`AUTONOMY_SHUTDOWN_DRAIN`, ON) and `TimeoutStopSec=90`.
- **WS-I economics** — the status snapshot's `economics` section (calls, $/day,
  cached share, uncached/output per call, p50/p90 latency, truncations, first-call
  cache hits, `budget_runway` when a balance was read) from `usage_records`; the
  loops section renders standing rail verdicts with a SINCE clock, `rail_cut_repeat`,
  and the goals section renders `yielded ×N · resumed ×M · deferred ×K`; the security
  section renders the data-home permission audit.

### Changed
- Corrections to the load-bearing numbers behind the 057 design, measured against
  production `usage_records`/telemetry: prompt caching IS live (80% of input tokens
  cached), latency is output-bound, and compaction was unreachable under
  `TASK_MAX_INPUT_TOKENS=200000`.

### Fixed (deployment)
- `polyrob.service`, `polyrob-email.service` and `polyrob-webview.service` now run with
  `UMask=0002`: with systemd's default 022 the WAL sidecars one unit created under the shared
  data dir were group-read-only, so a sibling unit hit `attempt to write a readonly database`
  (the email surface's outbound queue, twice in two days). Same fix the root helper units got
  on 2026-09-18; ratcheted by `tests/unit/test_shared_data_units_umask.py`.

### Added
- **`goal_ask`** — a rail that hits a question only the owner can answer now raises a
  durable board ask instead of asking once into a chat line that scrolls away. The ask is
  dedup-refreshed, visible on every owner seat, and the goal that raised it is HELD (not
  re-served) while it is open: on 2026-09-12 one rail re-fired seven times in 75 minutes
  while the owner was deciding, and a buyback rail stalled for 12 hours on a question that
  was asked once and never resurfaced.
- `polyrob cron edit` gains `--task-file <path>` (replace a job's prose without
  re-scheduling it), `--deliver <target|none>` and `--rig <name>`; `cron show|edit|cancel`
  accept a unique job-id prefix, and a rig error is reported separately from a job error.
- `OPENROUTER_REASONING_ENABLED=false` sends `reasoning.enabled=false` — measured as the one
  lever a DeepInfra upstream honours — and `OPENROUTER_REASONING_MAX_TOKENS` bounds the
  model's reasoning inside the output cap. A cut spent on REASONING gets its own note and
  its own count, so "the answer was truncated" and "the thinking was truncated" stop
  reading alike.
- Every OpenRouter call stamps `prefix_sha`/`tools_sha` onto its usage record, which makes a
  cache miss attributable to a changed prompt prefix rather than guessed at.
- The composed daily digest rolls up the owner messages a cap or a pause suppressed
  (window-bounded, oldest first, day-stamped, capped at 8); an unreadable store is named
  rather than read as "nothing was missed".
- `OPENROUTER_PROVIDER_SORT` (`latency`|`throughput`|`price`, default unset = byte-identical):
  asks OpenRouter to order upstreams for the SAME model by latency instead of its price-first
  default — an ops knob for a seat whose cheapest upstream stalls (four 290–410 s timeouts and
  five money-rail cap cuts in one afternoon on prod).

### Fixed

- `anysite_api`: an empty answer from a `/search/` endpoint to a query longer than two words now
  carries a hint that the endpoint silently rejects long phrasings and names the two-word retry —
  the agent had twice recorded a live theme as "exhausted" on a query-shape artefact.
- `scripts/deploy_when_idle.sh` also vetoes a deploy while an owner DM was routed in the last
  `OWNER_DM_VETO_SEC` (600) seconds, read from the `inbound_routed{tier=owner}` telemetry row
  the harness writes before any turn starts — independent of the turn marker, which failed
  twice on 2026-09-19. An unreadable telemetry DB counts as busy. `DEPLOY_WHEN_IDLE_DRY_RUN=1`
  exercises the decision without deploying (bash test).
- The live-turn marker (`turn.active`) now always lives under `<POLYROB_DATA_DIR>/locks/`, the
  location the deploy waiter, the cron tick and `polyrob doctor` read. The agent process sets
  `POLYROB_WORKSPACE_LOCK_DIR=<project>/.polyrob`, so the marker was landing there instead and
  two deploys (10:36Z, 18:29Z on 2026-09-19) fired in the middle of a live owner turn.
- `room_read` no longer lets an empty room ledger read as "my posts may not have rendered":
  Telegram never delivers a bot's OWN posts back as updates, so a channel only the bot posts
  to stays at 0 lines by design — both the room listing and the per-room read now say so and
  name the send receipt as the delivery confirmation (prod 2026-09-19: the agent told the
  owner for two days that channel posts were "receipt-only, visibility unconfirmed").
- `defi_data.swap_quote` cross-checks the route's output against the keyless indexer-implied
  output and renders `⚠ SUSPECT QUOTE` when they disagree by more than 5× either way (the
  `lifi:fly` cold-response constant of 2026-09-19); consistent quotes say so, and a missing
  or zero-liquidity price renders `cross-check: unavailable` — never a verdict.
- History dedup no longer replaces a SHORT identical tool result with a "[duplicate of …]"
  back-reference (`DEDUP_MIN_CHARS`, 160): three identical `Edited <file>` confirmations in
  one step were reported by the agent as "the dedup artifact" instead of three landed edits,
  and the pointer was longer than what it replaced.
- A screenshot of a never-navigated page (`about:blank` / new tab) is no longer attached
  to the state message: every step of a run that never touched the browser was shipping one
  useless image per call through the vision path of the metered seat (`agents/task/agent/
  prompts.py::is_blank_page_url`). A navigated page's screenshot is unchanged.
- The system prompt now states the executor's real per-step tool-call cap (5) instead of
  the config ceiling (10), and a deferred call's notice says it did NOT run and must be
  re-issued — a 6-call batch used to lose its 6th call from the agent's point of view.
- The GeckoTerminal client retries once (2 s) on a 429, and a provider error that survives
  the retry now propagates from the pool resolver: `defi_data_ohlcv` says "could not resolve a
  pool: HTTP 429" instead of "no indexed pool found … not an outage" (five batched candle
  reads were reported as absent pools on 2026-09-19).
- The X API tool remembers a `402 Payment Required` (credits depleted) for 60 min and refuses
  every further API call locally with the owner remedy instead of re-discovering it per
  endpoint (a round burned 52 calls in 15 min); a successful call clears the verdict.
- `defi_data_swap_quote` stamps `quoted_at` (UTC) and the pair addresses on every quote, so
  two re-quotes that happen to agree are never byte-identical and the history compaction
  pass no longer folds a fresh read into "[duplicate of an earlier tool result]".
- `defi_data_ohlcv` accepts a 32-byte pool id (Robinhood Chain singleton pools) for both
  the auto-resolved and an explicit `pool=`; the path validator treated every pool as a
  20-byte address, so every RH candle read was refused. A token address is still an address.
- `coding_grep` given a FILE path greps that file (it used to walk it as a directory and
  answer "(no matches)"); a path that is neither a file nor a directory errors naming it.
- `kb_ingest` no longer refuses the agent's own plain-text documents (`.md/.txt/.rst`)
  under a workspace `data/` directory purely by path: they are admitted when they sit
  outside the runtime data subtrees and a content secret-scan passes. The broad
  `is_secret_path` classifier is unchanged for every other consumer.
- The autonomous X post cooldown refusal now names the remaining window and the exact
  one-shot deferral (`cronjob_schedule` at an ISO instant) so a NEW executed-transaction
  report is deferred rather than dropped; a genuine re-fire is still told to skip.
- Goal dispatch no longer starves behind staggered money rails: with
  `GOAL_YIELD_FOR_MONEY_RAIL` on, a scheduled money-class cron job does not hold the
  board back through `GOAL_DISPATCH_CRON_HEADROOM_SEC` (it pre-empts a running goal
  instead); a job mid-run and every ops-class job still defer.
- The autonomous context budget gauges the HISTORY only — it no longer clamps the model's
  safety limit, which cut the first rail of a run at step 1.
- The state message honours `TOOL_RESULT_MAX_TOKENS` too. It reprinted every tool result
  raw under a separate 100k-character limit, so five large JSON answers put ~110k uncached
  tokens into a single step (210k in, a 221 s timeout) with the 6k cap already armed.
- A tool-less auxiliary LLM call no longer inherits the last step's toolset, and the
  native-tools billing path feeds the output-scaled timeout (it was sizing the budget from
  input alone).
- An LLM timeout inside an AUTONOMOUS run never DMs the owner: a background rail's retry is
  not owner news.
- `read_file` paged past the end of a file is an honest empty answer naming the line count,
  not a crash.
- `coding_grep`, `kb_ingest` and the tool-availability note keep their fixes from the same
  window; the compute gates now name the limb that is ACTUALLY unmet (production ran
  posture 1 with `CODE_EXEC_ENABLED=false` and the note still said "raise the posture").
- A credential verdict's re-probe hold doubles per repeat failure to a 6 h cap, and an SMTP
  verdict is keyed on a password change-detector digest, so a corrected password clears the
  verdict instead of waiting out a fixed TTL. A verdict READ never creates `verdicts.db`.
- Files and SQLite stores born BETWEEN deploys are group-writable from birth (one birth
  rule in `core/sqlite_util.py` and the session state writer). A store created 30 seconds
  after a deploy's ownership pass was born `0644` and degraded with "attempt to write a
  readonly database".
- A ledger or cooldown READ never creates `telemetry_events.db`, and a `/dapp` read never
  creates `dapp_sessions.db` — a read that plants a file makes "no data" and "never ran"
  indistinguishable.
- `polyrob doctor` reads the DEPLOYED owner tenant (refusing rather than guessing when the
  env file is unreadable) and its money section reads without a DI container: an unread
  ledger leg renders `unavailable`, never `$0.00`. `GET /api/webgate/doctor` names a failed
  report (`checks: null` + `checks_error`) instead of a confident empty list.
- An unreadable `./.polyrob/.env` candidate or an unreadable `.git` probe on the local
  bootstrap ladder is skipped, not a crash — both killed `polyrob owner asks` when it ran
  as the service user from another user's clone.
- Promoting an owner-approved draft no longer trips the claim-provenance guard: the owner's
  approval of a SOURCED draft is not an unsourced claim.
- A cron job that already told the owner during its run does not send the delivery echo
  again.
- `polyrob doctor` in a directory the agent has never run in creates NOTHING. Its new
  money section builds the unified ledger, whose caps block asks for the wallet policy
  gate, and two read-creates-a-store defects followed: constructing the wallet audit sink
  mkdir'd `<cwd>/.polyrob/wallet/` and minted an `audit.jsonl.lock` before reading a byte
  (both are now deferred to the first real write), and on a base install without the
  `crypto` extra the missing-dependency WARNING was enough to mint
  `<cwd>/.polyrob/logs/bot.log` (a base install has no wallet by design — that is not a
  warn-worthy condition). The clean-room wheel gate gained a test for the second, which
  the dev tree structurally cannot see.

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
  and a watchdog. A second instance gets its own `<name>.fly.env` + a container-specific
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

Releases before 1.0.0 (0.4.2 through 0.13.0) are in
[`CHANGELOG-archive.md`](CHANGELOG-archive.md).
