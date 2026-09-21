# Group chats (rooms)

POLYROB can join a group chat, supergroup, forum or channel — a **room** — and answer
there, alongside its normal one-to-one DM life. A room is many humans, some of them
strangers, so a room turn runs on a deliberately narrower rail than a DM: no owner
facts, no wallet, no memory recall, and a read-only toolset. This page is the complete
reference.

---

## What a room is

Three things make a room work, in order:

1. **Allowlist** — default-DENY. The agent accepts nothing from a group/supergroup/
   channel it has not been explicitly allowed into (`polyrob owner groups allow` or
   `/groups allow here`). An unlisted chat is dropped silently — nothing is stored,
   nothing is sent.
2. **Roles** — inside an allowed room, every sender carries a per-chat role: **owner**,
   **admin**, **member**, or **blocked**. A role decides who may steer the agent, who
   may run admin verbs, and who is denied outright — see [Roles](#roles) below.
3. **Mode** — a per-room setting (`chat.mode`) decides WHEN the agent answers: on a
   mention, on every message, on an addressed owner/admin only, or never. See
   [Behaviour (`chat.*` keys)](#behaviour-chat-keys) below.

---

## Prerequisites

- **`GROUP_CHAT_ENABLED`** (default OFF; ON under `AUTONOMY_MODE=autonomous`) is
  the master flag and the only one the room rail depends on. It does not need
  `CORRESPONDENT_ACCESS_ENABLED`.
- **`SINGULAR_CHAT_ENABLED`** must be on too: the room rail is *built* on that
  bus — the chat-to-session registry that decides which session a room resolves
  to, the reply caps, the ledger, and the outbound router that applies the
  `[SILENT]` rule and the secret scrub. Every `polyrob <surface>` daemon sets it.
  With `GROUP_CHAT_ENABLED` on and the bus missing, room messages are **denied
  silently** and one warning names the flag — the agent will not run a room
  half-built.
- ⚠️ **Rooms are fully useful on Telegram only.** The ledger reads a Telegram
  message id, so a Discord, Slack or Signal room records no lines: its context
  block stays empty, the agent answers each line with no history of the
  conversation, and a service job there always reports no-change. The agent logs
  one warning per surface naming this. Allowlisting, roles, modes and caps work
  on every surface; the *memory of the room* does not.

---

## Telegram setup: privacy mode

Telegram bots only see messages addressed to them in a group **unless** privacy mode
is off, and Telegram caches that setting at the moment the bot joins a chat. Before
using rooms on Telegram:

1. Open **@BotFather** → `/mybots` → your bot → **Bot Settings** → **Group Privacy** →
   turn it **OFF**.
2. **Remove the bot from the room and re-add it** (or invite it fresh) — a bot already
   in the room keeps the privacy state it joined with. The alternative that also works
   without a re-invite: **make the bot a room admin**, which always receives every
   message regardless of the privacy setting.
3. `polyrob telegram` prints a reminder of this at startup.
4. In the room, run `/groups allow here` (owner only) to allowlist it, then set a mode
   with `/groups mode here mention` (the default) or another mode.

`TELEGRAM_BOT_USERNAME` overrides the bot's own handle for mention detection when
`getMe` is unavailable at boot (the handle otherwise self-heals from the bot's own
outbound messages).

---

## Roles

| Capability | owner | admin | member | blocked |
|---|---|---|---|---|
| Line appended to the ledger | yes | yes | yes | yes (context only) |
| Mention / reply-to-bot / wake word triggers a turn | yes | yes | yes | no |
| Triggers without a mention (`chat.mode=active`) | yes | yes | judged by the agent | no |
| `/groups mode\|tail`, `/mute`, `/cancel`, `/new` in the room | yes | yes | no | no |
| `/groups allow\|deny\|set\|role\|service` (owner-only, confirmed by DM) | yes | no | no | no |
| Tools on the turn | room toolset | room toolset | room toolset | — |
| Money, self-mod, host, secrets, recall, `/pause`, `/dev`, wallet, trade, deploy | never in a room — the owner uses the DM | no | no | no |

The **owner** is the bound owner principal, resolved from the authenticated sender —
never from a display name. A member who renames themselves to look like the owner
still shows their real numeric id on every ledger line, so the owner's replies can
never be impersonated. `blocked` is the only per-member deny; there is no row for a
member who has done nothing — an unlisted sender defaults to `member`.

Grant/revoke a role with `/groups role here <numeric id> admin|member|blocked` (owner;
an admin may only set `blocked`) or `polyrob owner groups role <surface> <chat_id>
<user_id> <role>`. `/groups admins here` reads Telegram's own admin list as
**suggestions** — nothing is granted until the owner confirms each one explicitly.

---

## Behaviour (`chat.*` keys)

Each room has its own overlay, validated through the same preference machinery as a
tenant setting. Set a key with `/groups set here <key> <value>` or `polyrob owner
groups set <surface> <chat_id> <key> <value>` (`unset`/`-` clears it back to default).

| key | type | default | meaning |
|---|---|---|---|
| `chat.mode` | `mention`/`active`/`listen`/`off` | `mention` | See [Modes](#modes) below |
| `chat.name` | str, ≤64 chars | empty (falls back to the chat title, else the chat id) | Shown to the agent in the room context block |
| `chat.instructions` | str, ≤1500 chars | empty | Owner-authored standing instructions, threat-scanned on write, rendered into the room's `<surface>` paragraph |
| `chat.verbosity` | `terse`/`normal`/`detailed` | tenant default | Reply verbosity, this room only |
| `chat.tone` | str, ≤200 chars | tenant default | Free-text tone hint, this room only |
| `chat.language` | str, ≤32 chars | tenant default | Reply language, e.g. `en` or `pt-BR` |
| `chat.wake_words` | list, ≤10 patterns, ≤64 chars each | `[]` | Case-insensitive regexes that address the bot without an `@mention`; anchor a whole word with `\b` (`\brob\b`), or `rob` also wakes on `problem` |
| `chat.context_lines` | int, 0-100 | `30` | How many unanswered ledger lines the agent is shown per turn |
| `chat.reply_cap_per_hour` | int, 0-200 | `20` | Overrides `GROUP_REPLY_CAP_PER_HOUR` for this room |
| `chat.member_cooldown_sec` | int, 0-600 | `20` | Overrides `GROUP_MEMBER_COOLDOWN_SEC` for this room |
| `chat.quiet_hours` | `HH-HH` (0-23, local) | tenant `digest.quiet_hours` | Demotes the room to `listen` inside the window: an addressed owner or admin still gets an answer, a member does not, whatever `chat.mode` says |
| `chat.mute_until` | float (unix epoch) | `0` (not muted) | Written by `/mute`; demotes the room to `listen` until it elapses |

**Reserved, no row yet — `set` refuses them today:** `chat.persona`, `chat.tool_deny`,
`chat.member_verbs`, `chat.reply_mode`, `chat.thread_scope`. These are named in the
design for a later release; setting one now returns a clear "not yet a real key" error
rather than silently writing somewhere nothing reads.

A forum supergroup's topics each get their own bound session (Telegram's own topic id
threads the session key); the `chat.*` policy itself is set once for the whole chat —
there is no separate per-topic override in v1.

### Modes

Being **addressed** — an `@mention`, a reply to one of the agent's own messages, a
`chat.wake_words` hit, or (owner/admin only) a slash command like `/mute` — is required
at every mode but `active`. **The owner is not exempt**: he is a person in a room full
of other people, and an agent that answered his every aside would both interrupt the
room and bill him for it.

- **`mention`** (default) — answer an addressed line from anyone, the owner included.
- **`active`** — also answer a line the agent judges worth answering even when nobody
  addressed it.
- **`listen`** — read everything; answer an addressed owner or room admin only.
- **`off`** — ledger only, never a reply, not even from the owner (he changes the mode
  from a seat, not by shouting into the room).

A mute (`/mute here 2h`) or a `chat.quiet_hours` window temporarily demotes the room to
`listen`, whatever `chat.mode` says, while it lasts.

`GROUP_DEFAULT_MODE` (default `mention`) sets the mode for a room with no
per-chat overlay. `GROUP_REQUIRE_MENTION=false` is honoured for one release as an
alias for `chat.mode=active`; an explicit `chat.mode` or `GROUP_DEFAULT_MODE`
always wins.

---

## Caps

Three independent limits keep a room from being a spend or noise vector:

- **`GROUP_REPLY_CAP_PER_HOUR`** (default `20`, overridable per room via
  `chat.reply_cap_per_hour`) — max committed agent replies into one room per rolling
  hour. Counted on successful delivery only; over cap the reply is suppressed (logged,
  never queued, never sent twice). It also bounds what is **spent**: once the cap is
  reached, a room line is refused at routing and never becomes a paid model call at
  all. The owner's and an admin's control verbs (`/groups`, `/mute`, `/cancel`, …)
  still work in a capped room — that is how you stop the noise that spent it.
- **`GROUP_MEMBER_COOLDOWN_SEC`** (default `20`, overridable via
  `chat.member_cooldown_sec`) — minimum seconds between two triggers from the same
  sender (human or bot) in one room. A trigger inside the window is denied silently.
- **Bot-loop guard** (`GROUP_BOT_LOOP_MAX`=`20` / `GROUP_BOT_LOOP_WINDOW_SEC`=`300` /
  `GROUP_BOT_LOOP_COOLDOWN_SEC`=`600`) — a bot-authored line never triggers a reply on
  its own account, but two bots CAN still be mentioned into a loop by a third party;
  once a room sees `GROUP_BOT_LOOP_MAX` bot-authored triggers inside the window, every
  further bot trigger is refused for `GROUP_BOT_LOOP_COOLDOWN_SEC`. Humans are never
  counted here.

A reply that is exactly `[SILENT]` costs nothing — an `active`-mode judgement of
"nothing to say" doesn't spend the hourly cap. `/mute here <duration>` and
`chat.mode=off` are the room-level kill switch; the owner's global `/pause` (or
`/pause all`) stops room replies too.

---

## What the agent can and cannot do in a room

Every room turn — owner included — runs on a `SessionProfile.PUBLIC` session and a
fixed, read-only toolset (`GROUP_TURN_TOOLS`, default
`task,web_fetch,defi_data`). That list is a **bound, not a floor**: unlike a private session, a
room is never widened with the base defaults, so it has no `filesystem` and no
`browser`, and `GROUP_TURN_TOOLS=""` really does mean "can only chat". The env
can only ever NARROW that list: naming anything outside `task`, `web_fetch` and
`defi_data` drops it with a WARN, so a tool added to POLYROB next month is
refused from a public room by default and has to be named room-safe in
`core/surfaces/room_policy.py` before it can be reached from one. The
session carries none of the owner tenant's private state: no owner facts, no
SOUL/SELF docs, no tenant memory prefetch or write, no episodic digest, no
episode WRITTEN at the end either, no live-health note, no `<environment>` block,
no project context. There is no `skip_memory` switch behind this — every one of
those injectors asks `is_public_session()` and returns early, which is why the
list can only grow by adding a guard, never by flipping a flag.

**Never reachable from a room, whoever spoke:**

- **Money** — no wallet, trade, invoice, deploy, or any spend-adjacent read
  (`portfolio`, `positions`, `balances`, `reconcile`). Every action of every tool
  the capability table marks `money` is refused by name, whether or not the tool
  was ever loaded.
- **The owner's knowledge base** — no `kb_search`, `kb_ingest`, `kb_list`,
  `kb_remove`, or any other `kb_*` verb. The `knowledge` tool is not a room tool
  and its verbs stay denied even if `GROUP_TURN_TOOLS` names it.
- **Deferred execution** — no `goal_create`, `goal_cancel`, `cronjob_schedule`,
  `cronjob_cancel`, `skill_manage`, `self_context_manage`, `preferences`,
  `owner_doc_manage`, `load_tool`, `tool_manage_install`, `mcp_install`,
  `self_modify`. A room turn cannot plant something a later owner-tenant run
  would execute, and it cannot widen its own rig.
- **Cross-session recall** — no `session_search`, `memory_search`, `memory`,
  `contact_history`, `recent_activity`, `agent_status`, `insights`,
  `usage_summary`. A room reply can never surface the owner's DM or trading history.
- **Outbound elsewhere** — no `message`, `send_email`, `email_send`.
  `send_message` is the only speech verb, and it lands in this room only.
- **Delegation / control** — no `delegate_task`, `subtask`, `parallel_subtasks`,
  `autonomy_control`.
- **Every room-bound turn is stamped `turn_kind=group`**, which the same forged-turn
  predicate that protects self-wakes and delegation results treats as forged — a room
  turn can never auto-activate or promote a skill, and `tx_guard` refuses to sign for
  it even if a schema somehow slipped through.

**What IS reachable**, from the default room toolset: the room ledger's own
context, `web_fetch` (results arrive untrusted-wrapped, same as everywhere
else), the read-only `defi_data` market lookups that are not spend-adjacent, the
`task` TODO tool, `send_message` into this room, and `load_skill` — the agent may
*load* a skill the operator already installed, but it cannot write, patch,
promote or install one, and it cannot load a tool.

Owner-only output — approval prompts, the LLM-outage notice, failure breadcrumbs, the
bootstrap id reply, and admin-verb confirmations — is redirected to the owner's DM
instead of rendering in the room; the room either gets nothing or, for a verb the owner
ran there, a note that it was sent to his DM. Approval prompts never render in a room
at all, and a room turn cannot reach an approval-gated verb in the first place. Failure
breadcrumbs are capped at one per room per 30 minutes, so a room in a crash loop cannot
flood the owner's DM.

## Owner commands from inside a room

The owner's money, host and control verbs do not RUN from a room, even for the owner
himself. Redirecting the reply to his DM was never enough — the action still executed,
so a member who talked the owner into typing one got it, and a room is the one place a
shoulder-surfer or a screen-share is guaranteed. These are refused with a one-line note
in his DM:

`/trade` `/wallet` `/deploy` `/launch` `/bridge` `/dev` `/pause` `/halt` `/resume`
`/approve` `/reject` `/allow` `/deny` `/config` `/prefs` `/mcp` `/apps` `/invoices`
`/settle` — do these in the private chat.

Still reachable from a room, answered in the owner's DM: `/status` `/help` `/groups`
`/mute` `/cancel` `/new` `/goals` `/recap` `/journey` `/missed`.

---

## When a room starts a new session

A room's session ages out exactly like a DM's, on the same
`SESSION_RESET_MODE` / `SESSION_IDLE_MINUTES` / `SESSION_RESET_HOUR` policy: the next
line after the boundary starts a fresh session instead of resuming yesterday's. The
room's ledger is unaffected (it is the durable record); only the agent's own
conversation state resets. `/new` from the owner or a room admin does it on demand.

---

## The service job

A room doesn't need a live mention to get a catch-up pass: `/groups service here every
30m [max 3]` (owner only) creates a recurring cron job bound to that room's session key.
Stop it with `/groups service here off`. CLI equivalent: `polyrob owner groups service
<surface> <chat_id> --every 30m --max 3` (`--every off` cancels).

Each tick:

1. Reads the room ledger since its own `goal` checkpoint, skipping any line already
   marked answered (`GOAL_GROUP_MAX_REPLIES_PER_RUN` caps how much of what's left
   gets answered per run, default `3`). A tick costs nothing — `skipped/<reason>` —
   when the room's mode/mute/quiet-hours policy says don't run at all
   (`mode_off`, `listen`, `muted`, `quiet_hours`) or the filtered tail is empty
   (`no_change`, the same shape as the wake change-gate).
2. Picks the lines that ask something answerable, address the bot, or that
   `chat.instructions` says to handle; ignores chatter.
3. Replies through the room's own bound session — never a fresh session, never the
   owner's DM — threaded to the line each reply answers.
4. Advances its checkpoint so the next tick doesn't re-read the same lines.

The service job and a live triggered turn use two different mechanisms to avoid
double-answering the same line: a live turn stamps every line it was shown with
`answered_by` = that session (so the service run, reading `unanswered_only`, skips
it); the service run's own `goal` checkpoint then bounds how far back the NEXT
service run reaches. Both still obey `chat.reply_cap_per_hour` and
`chat.quiet_hours`.

---

## Retention and the bio disclosure

Every allowed-room line — text, media captions, edits, and service events (joins,
leaves, title and photo changes, pins) — is written to the room ledger before any other
gate runs, secret-scrubbed, with no media bytes stored. A service event is stored as
what it IS (`kind=service`, e.g. "joined the room"), never as a member line with no
text. A line from a chat the owner has NOT allowlisted is never stored and never
processed at all — it is dropped before the agent does any work on it. (On a
non-Telegram surface nothing is recorded at all — see [Prerequisites](#prerequisites).)

A line posted **anonymously** — a Telegram anonymous admin, or anything sent as
the chat itself — carries no principal, so it can never be routed: no role, no
command, no reply. It is still written to the ledger, as an anonymous row
(`sender_id` empty, the posting chat's title as the name, role `member`), so
the next room turn answers with the room's full record rather than around a
message everyone present can see.

Retention is bounded two ways: `GROUP_LEDGER_MAX_ROWS_PER_CHAT` (default `2000`, oldest
rows pruned first) and `GROUP_LEDGER_RETENTION_DAYS` (default `14`, wall-clock age,
pruned on every write and every read — a room that goes quiet still ages out). The
owner reads it back with `/groups tail here [n]` (DM'd) or `polyrob owner groups tail`;
nobody else can read the ledger back.

Because the agent is logging room messages, disclose it in the bot's profile. A
starting sentence for the bio:

> This bot logs messages in rooms it's added to, for up to 14 days, so it can follow
> the conversation. Message content is never used to train a model or shared outside
> this bot.

Adjust "14 days" if you changed `GROUP_LEDGER_RETENTION_DAYS`.

---

## Paid moderation actions (046)

A member can pay to mute or ban another member. The room's owner decides which
of those are on sale, at what price, for how long, and in what token.

**Turn it on.** The ONLY line that has to be typed in the room is the
allowlist; the rest is administration and belongs in your DM. From inside the
room, once:

```
/groups allow here my room
```

Then, in the DM, point `here` at it and configure:

```
/groups use telegram -1002002374383   # `here` now means that room, in this DM
/paid asset rob                       # an asset_id from `polyrob wallet asset list`
/paid price mute 0.50                 # USD; sized into the token at mint
/paid price ban 2.00                  # optional — a room may sell one and not the other
/groups set here paid_ban_max_duration 6h
/paid enable
/groups set here member_verbs help,mute,ban,unmute,unban
/paid status
```

⚠️ **A unit price is BOUND to the asset it was written for.** You type a bare
number and the room's current asset is appended, so the stored value is
`1500 pnl`. Change `chat.paid_asset` afterwards and that price REFUSES, naming
both assets — it is never re-denominated, and never quietly replaced by the USD
figure. Re-price the room, or switch the asset back.

`/groups use` shows the current focus, `/groups use none` clears it, and
`/groups list` gives you the chat ids. Every line above also works typed inside
the room, where `here` means that room whatever is focused — being in a room
always wins.

⚠️ Focus is a POINTER, not a permission. It decides which room `here` means and
nothing else: your role is still resolved for that room on every verb, so
focusing a room you are only a member of buys you nothing. It is per-person, so
one admin's `/groups use` never redirects another's `here`.

⚠️ A `chat.` prefix is optional in `/groups set` — `paid_ban_max_duration` and
`chat.paid_ban_max_duration` are the same key. An unknown key is still refused.

**Pricing in the token itself.** A USD price has to be converted into the
token at mint, which needs a live quote over a pool the screen will accept. A
young token fails that — not because anything is wrong with it, but because
`pool_screen.classify` answers *"should I buy this?"*, and ordinary launch churn
(volume far above depth) reads as WASH. Measured on PNL/Robinhood Chain,
2026-09-15: V/L 14.6 against a threshold of 10, at 36 hours old.

So a room may name the amount directly:

```
/groups set here paid_mute_units 1500     # 1,500 tokens of the room's asset
/groups set here paid_ban_units 3500
```

No quote, no screen, no oracle — the amount IS the price, and it is what the
member is quoted. ⚠️ **Keep the USD price set**: it stays the owner's declared
value and is what the ledger, `X402_INVOICE_MAX_USD` and your own reporting
read. A token price with no USD beside it is NAMED as such, because booking the
ledger at $0.00 would be a lie. The asset's `min_amount_raw` floor still
applies — pricing in tokens skips the oracle, never the floor.

⚠️ Every line matters. `/paid enable` REFUSES while nothing is priced — a room
that sells nothing reads as working and refuses every member who tries. And
without `member_verbs`, a member's `/mute` is not a command at all; it is
ordinary room chatter. `/paid status` warns you when that is the case.

⚠️ Paid actions also need `X402_INVOICE_ENABLED`. Without it the settlement
watcher never starts, so an offer would take real money and nothing would ever
detect it — an offer in that state is refused before the invoice exists, naming
the reason.

**An owner or admin can CHOOSE to pay** — add the word `pay`:

```
/ban 1h pay
```

Free is still the default: someone who already holds the authority should not be
taxed for using it. But that also means the person most likely to DEMONSTRATE
the rail is the one person who can never trigger it, so the free path is
opt-out, one word. ⚠️ Paying buys the payment FLOW, never authority — every
target protection is re-run, so an admin who pays still cannot touch the owner,
another admin, or himself. `chat.member_verbs` is not consulted either: that
gates members, and an owner needs no grant.

**Buy one** (member, replying to the post they want acted on):

```
/mute 1h
/ban 6h
```

⚠️ These verbs mean TWO things, separated by a fact rather than a guess. **In
reply to a message** they target that MEMBER. With **no reply**, `/mute` is the
older room verb that silences the AGENT for a while and the others simply say
they need a person. An owner or room admin replying acts FREE — they already
hold the authority, so charging them would be theatre — but the target
protections still apply to them.

The offer, with a QR payment card, is posted **in the room**, addressed to the
member who asked. So is the receipt when the effect lands.

**Buying the way out.** `/unmute` and `/unban` are priced separately
(`chat.paid_unmute_usd`, `chat.paid_unban_usd`) and are the only verbs the rail
lets anyone aim at THEMSELVES — buying your own way out is the one legitimate
self-target.

⚠️ In practice, on Telegram, somebody ELSE pays. A muted member cannot send a
message, so they cannot send `/unmute` either, and a banned member is not in the
chat at all. The self-target rule is the correct policy and it is what a surface
where a silenced member can still issue a command would need; today it is the
member's friend, or a room admin acting free, who lifts it. Lifting your own
mute from a DM is not built.

**Telling members** (member, in the room):

```
/help                            # what YOU can use here, with prices
/paid                            # the price list on its own
```

⚠️ `/help` in a room answers a plain MEMBER with the room's own short help — the
verbs this room granted, what each costs HERE, and the duration cap — posted in
the room. The owner's and a room admin's `/help` is unchanged. `/paid` is a
member verb too, but only if you grant it (`/groups set here member_verbs
help,mute,paid`); without the grant it is the owner's configuration verb and
nothing else.

Prices and caps in both come from the SAME readers the offer path uses, so a
member is never quoted a figure the purchase would refuse. A verb you granted
but never priced is NAMED with the reason rather than hidden — silence would
read as "that verb does not exist here" while every attempt to use it is
refused.

**What the agent is told.** A room that sells something adds a paragraph to the
model's `<surface>` block: which verbs are for sale, at what price, and that
they are commands a MEMBER TYPES. It is a description, never a capability — the
room session keeps its read-only 044 toolset and the invoice verb stays in
`ROOM_DENIED_ACTIONS`, so the agent still cannot mint, price or perform any of
it. The reason to say it at all is the opposite failure: asked "can you get him
muted?", a model that has never heard of the rail either denies a shipped
capability or invents a way to do it itself.

### What is refused, and why

| refusal | reason |
|---|---|
| the target is the owner, a room admin, a live Telegram chat admin, or the bot | money must not buy authority over the people who hold it — the chat admin check is a LIVE `getChatMember` read, not our own roles table, which only knows the rows we wrote |
| the owner cannot be identified on this surface | ⚠️ the whole sale is refused. "I could not find the owner" and "the owner is not this person" are different facts and only one is safe to act on |
| the target is you | except for `/unmute` and `/unban`, which are exactly the counter-pay case |
| the caps | per payer per day, and per target per day — one funded actor cannot run the room |
| the bot is not an admin here | ⚠️ checked BEFORE the invoice exists. We never sell what we cannot deliver |
| the price cannot be sized | a stale quote, a WASH or UNSCREENABLE pool, a pool below the asset's liquidity floor, or a price so high the fee would fall under the asset's token floor |
| the duration | above the room's `paid_<verb>_max_duration`, or above the catalog's own 30-day cap |
| paused | the 031 record, `social` scope |
| the rail cannot settle | invoicing is off, or this room's asset is on a chain nothing watches — checked BEFORE the mint |

⚠️ The last pricing case is the one worth understanding. The price is in USD but
the payment is in a token, so a RISING token price makes the fee smaller in
token terms. Anyone who could push a thin pool upward would buy the action for
dust. `polyrob wallet asset add --min-amount` sets the floor in raw token units
that stops it, and the pool screen refuses a market it cannot read honestly.

### When it cannot be applied

The offer settles, and then the effect fails — the bot lost its admin rights,
the target left, Telegram rejects the call.

⚠️ **You owe a credit, not a refund.** The payer is told plainly what failed —
in the room, where they are — and holds a credit good for one action of that verb
in that room at no further charge. The next time they ask for it, the credit is
spent instead of an invoice being minted. There is deliberately no automatic
outbound refund: that would be a money-SPEND verb needing a `tx_guard` intent and
the owner-approved lane. The credit discharges the obligation with no new spend
path.

A payment that arrives for an offer that is no longer open (expired or
withdrawn) is NOT applied. The room is told, and so are you — an offer we said
would not apply must not quietly apply.

Every credit owed appears in `/status` as a CRITICAL health item, in
`polyrob owner paid list`, and in `/paid offers`. It is money you HOLD against a
service that was never delivered.

### Seats

| seat | verbs |
|---|---|
| Telegram, in the room | `/paid status\|enable\|disable\|price <verb> <usd>\|asset <id>\|offers\|cancel <id>` |
| CLI | `polyrob owner paid list [--chat <id>]`, `polyrob owner paid show <chat_id>`, `polyrob owner paid cancel <offer_id>` |
| status | the `Paid room actions` section of `/status` and `polyrob doctor` |

`enable`/`disable`/`price`/`asset` are the owner's; `status`/`offers`/`cancel`
are reachable by that room's own admin too. A PAID offer cannot be cancelled —
that case is a credit.

### What this does NOT do

- The agent does not decide to mute anyone. A member asks, pays, and Telegram
  performs it.
- The room's LLM session is not on this path at all. It still has no money tool
  and cannot create an invoice; `/mute` is a deterministic command, so it works
  when the model is down, out of credit, or paused.
- Nothing here can move money OUT. The only money verb involved creates a
  receivable.

## Flags

Every default this page names is the shipped one. Paid actions add
`ROOM_ACTIONS_ENABLED` (default OFF) on top of the per-room `chat.paid_*` keys;
the assets they can be priced in come from `PAYMENT_DEFAULT_ASSET` and
`polyrob wallet asset`. `ROOM_ACTION_CARD_ENABLED` (defaults to
`ROOM_ACTIONS_ENABLED`) renders the QR payment card; `ROOM_ACTION_OFFER_TTL` is
only the fallback for a room that set no `chat.paid_offer_ttl`. An offer and its
invoice share ONE deadline. To see the whole `GROUP_*`
family with its resolved values and sources on your box, run `polyrob doctor
--flags --search GROUP_` (the catalog group is *Identity / polyrob / local
profile*); the reference with a code anchor per row is
[`../CONFIGURATION.md`](../CONFIGURATION.md).

---

## Owner verbs, at a glance

| Verb | Seats | Effect |
|------|-------|--------|
| `/groups allow here [name]` · `polyrob owner groups allow <surface> <chat_id>` | Telegram (in the room), CLI | Allowlist the current room |
| `/groups deny here` · `polyrob owner groups deny <surface> <chat_id>` | Telegram, CLI | Revoke; ledger kept until retention |
| `/groups list` · `polyrob owner groups list` | Telegram, CLI | Rooms, mode, replies today, last write |
| `/groups mode here <mode>` · `polyrob owner groups mode <surface> <chat_id> <mode>` | Telegram (owner/admin), CLI | Set `chat.mode` |
| `/groups set here <key> <value>` · `polyrob owner groups set <surface> <chat_id> <key> <value>` | Telegram (owner), CLI | Set any `chat.*` key |
| `/groups role here <id> <role>` · `polyrob owner groups role <surface> <chat_id> <user_id> <role>` | Telegram (owner; admin may only set `blocked`), CLI | Grant a per-chat role |
| `/groups admins here` | Telegram only (needs the live bot connection) | Telegram's own admin list as suggestions, confirmed one by one |
| `/paid status\|offers\|cancel <id>` · `polyrob owner paid list\|show\|cancel` | Telegram (owner/admin), CLI | Read this room's paid actions; withdraw a PENDING offer |
| `/paid enable\|disable\|price <verb> <usd>\|asset <id>` | Telegram (owner) | Configure what this room sells, at what price, in what token |
| `/mute <duration>` · `/ban <duration>` · `/unmute` · `/unban` IN REPLY | Telegram | Act on that member — free for the owner and room admins, a paid action for a member; a muted or banned member may counter-pay their own `/unmute` or `/unban` |
| `/groups tail here [n]` · `polyrob owner groups tail <surface> <chat_id>` | Telegram (owner/admin), CLI | Last n ledger lines, DM'd |
| `/groups service here every 30m [max 3]` · `polyrob owner groups service <surface> <chat_id> --every 30m --max 3` | Telegram (owner), CLI | Start/stop the recurring catch-up job |
| `/mute here 2h` | Telegram only (owner/admin) | `chat.mode=listen` with an expiry |

The status snapshot (`/status`, `polyrob doctor`, the console) carries a `groups`
section: rooms served, mode, replies today, cap hits, ledger size, last write.
