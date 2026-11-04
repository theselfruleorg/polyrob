---
name: self-deploy
description: 'How to bring a fresh instance of yourself online: assess what is already configured (model, wallet, funding, email, X, autonomy grants), provision what you can yourself (own inbox, standing work), ask the owner only for what needs a human (API keys, funding, a new social account, a CAPTCHA), and report one honest readiness table before you start operating'
license: MIT
metadata:
  polyrob-when: 'Bringing a new instance of yourself online'
  polyrob-priority: '1'
  polyrob-owner-only: 'true'
  polyrob-version: '2'
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
   thing yourself (an inbox, a goal, a cron job), pull it. An X account is
   NOT such a lever — see Step 2. Ask only for what needs a human: a secret
   you cannot mint, money, a CAPTCHA (you never solve one), a setting (the env
   files are hard-denied to you; `propose_setting` files the change for the
   owner, and it applies at the restart the owner sends with `/restart`).
3. **Report facts, not hopes.** Every line of the readiness table is one of
   `ready` / `missing → <exact remedy>` / `unverified (<reason>)`. A check
   that did not run is not a check that passed.

## Step 1 — Read your own state

Run these and keep the raw lines; they are the evidence for the table.

| What | Verb | What to read |
|---|---|---|
| Config + loops + wallet balance | `agent_status` | `posture:` (mode/local/compute), `autonomy_loops:`, `wallet:`, `wallet_addresses:` |
| Identity (email, X API, avatar, ERC-8004) | the `identity` lines of `agent_status` (or ask the owner for their `/status` on a chat seat) | `email:` ready or `none → remedy`; `x:` `api configured @handle` / `PARTIAL — missing …` / `no api keys` |
| Which tools you actually hold | the `<tool-catalog>` in your context | `loaded` / `loadable` / `gated:<reason> + remedy`. A `gated` money tool is an OWNER decision — never a thing to work around |
| Wallet addresses, both families | the `wallet_addresses:` line of `agent_status` (`x402_pay_x402_wallet_status` only when `x402_pay` is already loaded — `load_tool` never loads a money tool) | the EVM address and the SEPARATE Solana address. Read them from the tool; never from memory |
| On-chain balances per chain | `defi_data_portfolio(chain=…)` for each money chain you will use | the native-coin row (`ETH …`, `SOL …`): a number, `UNKNOWN` (read failed) or `EMPTY` (a real zero) — three different facts |
| X account | the `x:` identity line above; if the X pack is loaded, its `x-engagement` skill names the checks | the handle, or what is missing |
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

**A social account (X).** Only if the X pack is loaded: load its
`x-engagement` skill and follow its account-setup part. A new account is the
owner's decision; you never solve a CAPTCHA.

**Standing work.** Only if `goals`/`cron` show as loaded AND the owner has
told you what your mission is. Create the goal(s) with `goal_create`
(whether a goal you write yourself may carry a money tool is the owner's
money regime). Trading runs only under a mandate the owner starts with
`/trade start` (`trading-mandate`); other money work the owner grants with
`/job new … tools=defi_trade`. Arm any monitor loop with
`cronjob_schedule` (the `standing-work` skill: goal or job, and the $0 watch
shape). Never seed work the owner did not name.

**Identity docs.** If the owner gives you durable facts or rules about how
they want you to operate, write them through `owner_doc_manage` — never as
loose files. The write is always queued in `/pending` as a draft for the
owner to activate. Report what the result says.

## Step 3 — Ask the owner, once, for exactly what is missing

File every missing SETTING with `propose_setting` first, one call per setting:
it puts a one-tap card in front of the owner for a console setting and files
the operator half as one ask, deduped by line, so the same line is never
asked twice. It needs a live owner turn: in an autonomous run, put the
settings in the ask below instead. Then raise ONE `owner_ask` for the rest — keys, funding, a
CAPTCHA (a durable ask the owner answers with
`/fulfill` or in chat; in an autonomous run, never a question in a message —
the run cannot wait for a reply). Its shape is fixed by the tool:

- `question` — ONE question, at most 600 characters, answerable in a word or
  two ("Can you add the missing keys and fund the wallet? A) today B) later").
- `why` — the list (at most 1200 characters): per item, what is missing, why
  you need it, the exact key name or owner verb, and what you will do the
  moment it lands.

In a live chat with the owner, also send the same list once with
`send_message`. Never name a `polyrob …` shell command on a chat seat.
Typical items:

- **Model key** — if `agent_status` shows no usable provider you would not be
  running; skip. If a cheaper aux model is wanted, name the flag
  (`AUX_MODEL_JUDGE` / `AUX_MODEL_COMPACTION`).
- **Funding** — give the EXACT addresses from the `wallet_addresses:` line of
  `agent_status`, per chain,
  and say which asset is gas on each (ETH on Base/Ethereum, SOL on
  Solana, POL on Polygon). Say what you plan to do with the first dollars.
  Never quote an address from memory.
- **`AGENTMAIL_API_KEY`** — if no email lever exists at all.
- **X API keys** — the names the `x-engagement` skill gives (X pack only).
- **Settings** — not in the ask: each one went through `propose_setting`
  above, unless this is an autonomous run (any `gated:<reason>` tool the
  mission needs; take the setting from the remedy text verbatim).
  Money settings (`DEFI_TRADE_ENABLED`,
  `SOLANA_TRADE_ENABLED`, `LAUNCHPAD_ENABLED`, `DEFI_AGENT_AUTONOMY`,
  `DEFI_AUTONOMOUS_TURN_TRADING`, `DEFI_TIERED_SPEND_LANE`) and caps
  (`DEFI_AUTONOMOUS_MAX_USD`, `WALLET_DAILY_CAP_USD`,
  `AGENT_WALLET_MAX_PER_TX_USD`) are the owner's alone. State what each one
  unlocks; never argue for a higher cap.
- **A CAPTCHA / phone check** a signup paused on.

Then stop and wait. Do not loop on the ask, do not re-send it, do not create
a goal to "wait for keys".

## Step 4 — Verify what landed, then report

When the owner says it is done (or after a restart), re-run Step 1. Anything
the owner claims to have set must be CONFIRMED by a read — `twitter_whoami`
for X keys, `email:` for the inbox, `defi_data_portfolio` for funding — before
it moves to `ready`. Then send the readiness table once, in this shape:

```
READINESS — <instance> @ <date>
model        ready      <provider/model from agent_status>
wallet       ready      evm 0x…  solana …   (<chain> $X gas Y · … · solana N SOL)
email        ready      <address>
x            ready      api @handle, writes ON · browser rail: off (owner's choice)
autonomy     ready      mode=autonomous posture=full goals=on cron=on
trading      ready      <money tools loaded>; ceiling $A/tx, $B/day
standing     ready      goal "<title>" · job "<task>" every Nh
pending      —          <anything still on the owner's list, or "nothing">
```

A row that is not `ready` says `missing → <remedy>` or `unverified (<why>)`.
Write the same table to a workspace file (`filesystem_write_file`; when the KB
is on, a file you write joins its `written` collection) so the next session does not redo
the interview.

## What this skill never does

- Never reads, prints or asks for a secret VALUE (`*_API_KEY`, seeds, tokens).
  Presence is the fact; the value is the owner's.
- Never edits an env file, a `streams.yaml`, or a service unit — all are
  hard-denied to you and an attempt is an incident, not a workaround.
- Never registers a SECOND social account or a second inbox. One identity per
  instance; if one exists, use it.
- Never treats a `gated` money tool as a bug to route around, and never
  creates a goal whose purpose is to be granted a tool.
- Never claims readiness from an owner's message alone. A read is the proof.
