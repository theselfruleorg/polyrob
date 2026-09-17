---
name: self-deploy
description: 'How to bring a fresh instance of yourself online: assess what is already configured (model, wallet, funding, email, X, autonomy grants), provision what you can yourself (own inbox, X account, standing work), ask the owner only for what needs a human (API keys, funding, a CAPTCHA), and report one honest readiness table before you start operating'
license: MIT
metadata:
  polyrob-priority: '1'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["x_browser_x_signup_start","x_browser_x_login_check","x402_wallet_status","twitter_whoami","agent_status"],"keywords":["set yourself up","set up yourself","initialise yourself","initialize yourself","self deploy","self-deploy","bootstrap","onboarding","are you ready","readiness","fresh instance","new instance","first run","provision your","create your x account","your own email","fund you","funding address"],"task_patterns":["(set|setting) (your|its)?self up","(initiali[sz]e|bootstrap|provision) (yourself|the (agent|instance))","(are you|is (it|everything)) (ready|configured|set up)","readiness (check|table|report)","what (do you|does it) (need|still need) (from me|to (start|run|operate))"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Self-deploy — bring yourself online

Run this when you are a NEW instance, when the owner says "set yourself up" /
"initialise" / "are you ready", or when a status read shows a missing
identity (no email, no X, empty wallet). Setup is a ONE-TIME ceremony with a
human on the other end; do it in one conversation and write down the result.

Three rules for the whole procedure:

1. **Assess before you ask.** Read your own state with tools first. Never ask
   the owner for something a tool can tell you, and never ask twice for a
   thing you were already given.
2. **Provision before you ask.** If a lever exists that lets you create the
   thing yourself (an inbox, an X account, a goal, a cron job), pull it. Ask
   only for what needs a human: a secret you cannot mint, money, a CAPTCHA,
   an env flag (flags are frozen at process start and the env files are
   hard-denied to you — an env change is ALWAYS an owner edit + restart).
3. **Report facts, not hopes.** Every line of the readiness table is one of
   `ready` / `missing → <exact remedy>` / `unverified (<reason>)`. A check
   that did not run is not a check that passed.

## Step 1 — Read your own state

Run these and keep the raw lines; they are the evidence for the table.

| What | Verb | What to read |
|---|---|---|
| Config + loops + wallet balance | `agent_status` | `posture:` (mode/local/compute), `autonomy_loops:`, `wallet:`, `wallet_addresses:` |
| Identity (email, X API, avatar, ERC-8004) | the `identity` lines of `agent_status` (or ask the owner for `polyrob doctor`) | `email:` ready or `none → remedy`; `x:` `api configured @handle` / `PARTIAL — missing …` / `no api keys` |
| Which tools you actually hold | the `<tool-catalog>` in your context | `loaded` / `loadable` / `gated:<reason> + remedy`. A `gated` money tool is an OWNER decision — never a thing to work around |
| Wallet addresses, both families | `x402_wallet_status` | the EVM address and the SEPARATE Solana address. Read them from the tool; never from memory |
| On-chain balances per chain | `defi_data.portfolio(chain=…)` for each money chain you will use | the `gas (…)` row: a number, `UNKNOWN` (read failed) or `EMPTY` (a real zero) — three different facts |
| X browser session | `x_login_check` (if `x_browser` is loaded) | logged in as @handle, or no session |
| X API rail | `twitter_whoami` (if `twitter` is loaded) | the authenticated handle, or "missing credentials" |
| Standing work | `goal_list` / `cronjob_list` (if loaded) | an EMPTY board on a fresh instance is expected, not a stall |

If a verb is not loaded, say so in the table as `unverified (tool not loaded)`
— do not infer the answer.

## Step 2 — Provision what you can

Do these in order. Each is idempotent; a second run must not create a second
thing.

**Email.** If `email:` says `none` but names `AGENTMAIL_API_KEY is set`, the
inbox appears the first time the `email` tool initialises — load it
(`load_tool("email")` if it is `loadable`) and re-read. If the key is NOT set,
this is an owner ask (see Step 3). You need your own address before an X
signup, and the owner needs it to know who is writing.

**X account.** Decide by what is present:

- `x: api configured @handle` → done. The API rail is the one that can post
  **polls**, search, and read mentions; prefer it for everything.
- X API keys absent, `x_browser` loadable and you have an email → call
  `x_signup_start`. It is ALWAYS owner-queued (creating an identity is the
  owner's decision) — the owner approves once. It WILL pause on a CAPTCHA
  or a phone check; that pause is the design, not a failure. Report exactly
  what the result says: the live @handle, whether the handle you requested
  was applied, and whether the automation disclosure reached the bio. If it
  says the disclosure was NOT applied, put that on the owner's list —
  X's automation rules want it on the profile before you post.
- After the account exists, polls and search still need API keys. Tell the
  owner in one paragraph: sign in to developer.x.com AS the agent's account,
  create a project + app with **Read and write** user permissions, generate
  the OAuth 1.0a **Access Token and Secret** for that account, and set
  `TWITTER_API_KEY`, `TWITTER_API_SECRET_KEY`, `TWITTER_ACCESS_TOKEN`,
  `TWITTER_ACCESS_TOKEN_SECRET` (+ `TWITTER_BOT_USERNAME`) in the instance
  env, then restart. A pay-per-use tier is enough; writes cost ~$0.015 each.

**Standing work.** Only if `goals`/`cron` show as loaded AND the owner has
told you what your mission is. Create the objective(s) with `goal_create`
(an objective/goal you write yourself can never carry a money tool — that is
by design; the owner grants money tools with `polyrob goals create --tools`
or `/trade`). Arm any monitor loop with `cronjob_schedule`. Never seed work
the owner did not name.

**Identity docs.** If the owner gives you durable facts about who you are or
how they want you to operate, write them through `owner_doc_manage` /
`self_context_manage` / `preferences(contract_propose=…)` — never as loose
files. They land in `.pending/` for the owner to promote; say so.

## Step 3 — Ask the owner, once, for exactly what is missing

Bundle every human-only item into ONE message. Per item: what is missing,
why you need it, the exact key name or command, and what you will do the
moment it lands. Typical items:

- **Model key** — if `agent_status` shows no usable provider you would not be
  running; skip. If a cheaper aux model is wanted, name the flag
  (`AUX_MODEL_JUDGE` / `COMPACTION_MODEL`).
- **Funding** — give the EXACT addresses from `x402_wallet_status`, per chain,
  and say which asset is gas on each (ETH on Base/Robinhood/Ethereum, SOL on
  Solana, POL on Polygon). Say what you plan to do with the first dollars.
  Never quote an address from memory.
- **`AGENTMAIL_API_KEY`** — if no email lever exists at all.
- **X API keys** — as above.
- **Env flags** — any `gated:<reason>` tool the mission needs. Name the flag
  from the remedy text verbatim. Money flags (`DEFI_TRADE_ENABLED`,
  `SOLANA_TRADE_ENABLED`, `LAUNCHPAD_ENABLED`, `DEFI_AGENT_AUTONOMY`,
  `DEFI_AUTONOMOUS_TURN_TRADING`, `DEFI_TIERED_SPEND_LANE`) and caps
  (`DEFI_AUTONOMOUS_MAX_USD`, `WALLET_DAILY_CAP_USD`,
  `AGENT_WALLET_MAX_PER_TX_USD`) are the owner's alone. State what each one
  unlocks; never argue for a higher cap.
- **A CAPTCHA / phone check** the X signup paused on.

Then stop and wait. Do not loop on the ask, do not re-send it, do not create
a goal to "wait for keys".

## Step 4 — Verify what landed, then report

When the owner says it is done (or after a restart), re-run Step 1. Anything
the owner claims to have set must be CONFIRMED by a read — `twitter_whoami`
for X keys, `email:` for the inbox, `defi_data.portfolio` for funding — before
it moves to `ready`. Then send the readiness table once, in this shape:

```
READINESS — <instance> @ <date>
model        ready      <provider/model from agent_status>
wallet       ready      evm 0x…  solana …   (base $X gas Y · robinhood gas Z · solana N SOL)
email        ready      <address>
x            ready      api @handle, writes ON · browser session @handle
autonomy     ready      mode=autonomous posture=full goals=on cron=on
trading      ready      defi_trade/solana_swap/launchpad loaded; ceiling $A/tx, $B/day
standing     ready      objective "<title>" · cron "<task>" every Nh
pending      —          <anything still on the owner's list, or "nothing">
```

A row that is not `ready` says `missing → <remedy>` or `unverified (<why>)`.
Write the same table to memory (`memory add` if the tool is loaded, else your
KB) so the next session does not redo the interview.

## What this skill never does

- Never reads, prints or asks for a secret VALUE (`*_API_KEY`, seeds, tokens).
  Presence is the fact; the value is the owner's.
- Never edits an env file, a `streams.yaml`, or a service unit — all are
  hard-denied to you and an attempt is an incident, not a workaround.
- Never registers a SECOND X account or a second inbox. One identity per
  instance; if one exists, use it.
- Never treats a `gated` money tool as a bug to route around, and never
  creates a goal whose purpose is to be granted a tool.
- Never claims readiness from an owner's message alone. A read is the proof.
