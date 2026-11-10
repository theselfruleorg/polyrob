# Chat commands

A slash command is a short order you type in a chat with the agent. The same
commands run on every seat you talk to the agent from: the terminal REPL
(`polyrob`), Telegram and the other chat apps, and the web console. The
"Runs on" column names the exceptions — a command that only the terminal has, or
one that is refused inside a group room.

You never have to use a command. Plain words work ("schedule a check every
morning at 9", "what did you spend this week"), and the agent uses its own tools.

- `/help` lists the commands of your seat, grouped as below.
- `/help <verb>` explains one command, with its current syntax and examples.
- A command a pack adds (such as `/x`) exists only while that pack is loaded.
- In a Telegram group, `/admin <what to do>` runs what the owner granted: see [Group chats](groups.md#admin-abilities-admin).

Shell commands (`polyrob goals`, `polyrob owner`, …) are on
[cli.md](cli.md).

---

## Commands

The tables below are generated from the verb table in the code; the syntax
column is a summary, and `/help <verb>` on your seat is the full form.

<!-- BEGIN GENERATED chat-commands -->

### Talk

| Command | What it does | Runs on |
|---|---|---|
| `/start` | A welcome and a short tour of what I can do | every seat |
| `/new [name]` | Open a separate chat beside the main one | every seat |
| `/chats` (`/sessions`) | Your chats: the main one and each separate one | every seat, not in a room |
| `/chat <number or name>` | Switch to another of your chats | every seat, not in a room |
| `/rename <name>` | Name the chat you are in | every seat, not in a room |
| `/close [number or name]` | Close a separate chat and go back to the main one | every seat, not in a room |
| `/clear` | Clear this chat and keep my instructions | terminal |
| `/compact` (`/compress`) | Shorten this chat's history in the background | terminal |
| `/history` | The turns of this chat | terminal |
| `/attach <path> [path…]` | Attach local files or images to my next step | terminal |
| `/steer <guidance>` | Give the running turn one more instruction (in a chat app, just send it) | terminal |
| `/session` (`/info`) | Who and where this chat is: owner, model, memory, files | terminal |
| `/replay <session-id>` | Replay a chat's feed | terminal |
| `/export <format> [output]` | Export this chat's data | terminal |
| `/task <goal>` | The old form of /new: a separate chat that starts with your goal | chat apps |

### Needs you

| Command | What it does | Runs on |
|---|---|---|
| `/inbox [all\|n]` | Everything waiting on a decision from you, blocking first; /inbox all lists every item | every seat |
| `/pending [show\|approve\|reject <kind> <id>]` | Every approval waiting on you: spends, contacts, tokens, and proposals | every seat |
| `/approve [<id>\|all]` | Approve something that is waiting on you | every seat, not in a room |
| `/reject [<id>\|all]` | Decline something that is waiting on you | every seat, not in a room |
| `/gates list \| add <action> \| remove <action>` | Which actions need your approval | terminal |
| `/asks [<id>]` | What I need from you to unblock work; /asks &lt;id&gt; opens one in full | every seat |
| `/missed [n]` | Messages I could not deliver live: capped, paused, or undelivered | every seat |
| `/fulfill <id> [what you want to tell the run]` | Mark an ask fulfilled so its work can continue | every seat, not in a room |
| `/cards [<id> [ok\|no\|re\|1-6]]` | Open action cards: confirm a quote, answer a choice, or cancel; /cards &lt;id&gt; reopens one in full | every seat, not in a room |

### Work

| Command | What it does | Runs on |
|---|---|---|
| `/goal new <what> · <show\|retry\|pause\|stop [--forever]\|allow> <id>` | Add, show, retry, grant, pause, or stop one goal | every seat, not in a room |
| `/goals` | The goal board summary | every seat |
| `/job [list\|new\|show\|edit\|pause\|resume\|run\|stop] …` (`/jobs`) | Scheduled jobs: the list, or add, change, grant, pause, resume, run, or stop one | every seat, not in a room |
| `/apps [list\|show\|approve\|reject\|kill\|start\|remove\|logs <slug>]` | Deployed apps: approve an address, check health, stop, start or remove one | every seat, not in a room |
| `/skills [words \| <id>]` | The skills I can load, and what each one does; /skills &lt;id&gt; shows one skill's text. You install or approve a skill in the terminal | every seat, not in a room |
| `/learn <description>` | Describe how to do something and I save it as a skill | terminal |
| `/todos` | The todo list in my working files | terminal |
| `/subagents` | What I can hand to a helper | terminal |

### Money

| Command | What it does | Runs on |
|---|---|---|
| `/book [retire <chain> <address> [go]]` | My ledger against every money chain: one verdict, then what disagrees | every seat |
| `/wallet [balances\|tokens\|signed [n]]` | Wallet addresses, network, and spend caps; set the no-ask ceiling or trust a token | every seat |
| `/invoices [status] \| refunded <id> [tx]` | What I have billed and who owes me | every seat |
| `/settle <id> [tx]` | Mark an invoice paid | every seat |
| `/trade start\|status\|pause\|resume\|stop\|release` | Trading: start it with one decision, see it, pause, resume, stop, or release a loss stop | every seat |
| `/offer list\|show\|orders\|quote\|resolve\|pause\|resume\|retire` | What I sell: offers, their orders, quotes, disputes; pause, resume or retire one | every seat |
| `/bridge <from> <to> <amount> [from usdc] [as <asset>] [go]` | Move value between chains: ETH/native or USDC in; native, WETH or USDC out | every seat |
| `/launch <SYMBOL> <name…> [logo <url>] [desc <text>] [buy <amount>] [go]` | Launch a token on the launchpad | every seat |
| `/deploy <SYMBOL> <supply> <name…> [on <chain>] [decimals <n>] [go]` | Deploy a fixed-supply token | every seat |
| `/lp positions\|pool\|quote\|add\|remove\|collect` | Liquidity positions: inspect, quote, add, remove, or collect fees | every seat |
| `/claim <token> [go]` | Collect the creator fees a launchpad already owes me | every seat |
| `/nft [send <id> <to> [go]]\|list\|info <contract> <id>\|account [<id>]\|transfer <contract> <id> <to> [go]\|revoke <contract> <operator> [go]` | Collectibles I hold: look, trust a collection, send one, or revoke an approval | every seat |
| `/dapp list\|revoke <id>` | Web pages my wallet is connected to, and how to cut one off | every seat |
| `/paid <status\|enable\|disable\|price <verb> <usd>\|asset <id>\|offers\|cancel <id>>` | Paid room actions: status, pricing, and offers | every seat |
| `/pay <url> [max_usd] [go] [id=<name>]` | Pay for one x402 resource, up to a price you name | every seat |
| `/swap <amount> <native\|token> to <token> on <chain> [slippage <bps>] [max <usd>] [go]` | Swap one token for another: a quote with the minimum you receive, then confirm | every seat |
| `/send <amount> <native\|token-address> to <address> on <chain> [max <usd>] [go]` | Send native value or a token to an address: a quote, then `go`. A bare amount is tokens; `$22` is dollars, which the agent converts | every seat |
| `/writeoff <chain> <address> [go] [reason]` | Write a holding off: the loss is its recorded cost, and nothing is sold | every seat |
| `/unquarantine <chain> <address> [go]` | Undo the quarantine of a look-alike holding, so it counts as open again | every seat |
| `/check` | Look at any address or ticker: a wallet's holdings or a token's report | every seat |

### Control

| Command | What it does | Runs on |
|---|---|---|
| `/cancel` (`/stop`) | Stop the task I am running now | every seat |
| `/pause [word…] [for 6h]` (`/halt`) | Stop autonomous work now, all of it or one scope | every seat, not in a room |
| `/resume [word…]` | Lift the pause | every seat, not in a room |
| `/run [pause\|resume\|stop <n>]` | Pause, resume or stop one background run | every seat, not in a room |
| `/mode` | What I may do without you, what I do when you are away, and how to change it | every seat |
| `/dev [message]` | Pass a message to an operator-run maintenance script, if this host has one | every seat, not in a room |
| `/restart [now]` | Restart me once my current work is done | every seat, not in a room |
| `/update [pre]` | Update me to the latest release, then report back (/update now: a waiting deploy stops waiting for your chat to go quiet) | every seat, not in a room |
| `/autonomy [on\|off] [--global]` | My background loops and what they hold; switch them on or off | terminal |

### Remember

| Command | What it does | Runs on |
|---|---|---|
| `/thread [n \| <hours>h]` | Our conversation across every session and rail, newest last | every seat, not in a room |
| `/journey [window]` (`/recap`) | What I have done over a recent window | every seat |
| `/rules` | Your rules for me and my operating contract, numbered | every seat, not in a room |
| `/kb [list [collection] \| search <query> \| <words>]` | Search and list my knowledge base | every seat |
| `/memory [search <query> \| scopes]` | Recall across sessions and inspect memory scopes | every seat, not in a room |
| `/self` (`/soul`) | My identity documents, read-only | every seat, not in a room |

### Look

| Command | What it does | Runs on |
|---|---|---|
| `/more <token>` | The rest of a list or text I cut short | every seat, not in a room |
| `/status` | Health first, then session, goals, loops, and wallet | every seat |
| `/files [n]` | Recent files I produced | every seat |
| `/cwd` | The directory I am working in right now | every seat |
| `/meter` (`/tokens`) | This turn's meter | terminal |
| `/contacts [<surface> <address>]` | Who I have been writing to, and the transcript with one of them | every seat, not in a room |
| `/why [n]` | The last things I refused to do, and the reason for each | every seat, not in a room |
| `/doctor` | The health report | terminal |
| `/logs` | Recent log lines of this chat | terminal |
| `/context` | What fills my context, slot by slot | terminal |
| `/steps` | The steps of the last turn | terminal |
| `/tools` | My abilities, built in and optional | terminal |
| `/telemetry` | Event counts and wallet spend across chats | terminal |
| `/usage [days]` (`/cost`) | What my AI use cost: by kind of work, job, model and day | every seat |
| `/finance [days]` | Income, spend, money still due and net, with my AI cost | terminal |

### Set up

| Command | What it does | Runs on |
|---|---|---|
| `/allow <surface> <target>` | Allow me to message a target | every seat, not in a room |
| `/deny <surface> <target>` | Revoke a message permission | every seat, not in a room |
| `/allowlist` | Who I am allowed to message | every seat |
| `/pair [list\|approve <surface> <code>\|revoke <surface> <id>]` | Strangers who asked to talk to me: list, approve or revoke | every seat, not in a room |
| `/groups <allow\|deny\|list\|use\|mode\|set\|role\|roles\|tools\|audit\|tail\|service\|admins> here\|<surface> <chat_id> [...]` | Room presence admin: allow, deny, mode, roles, and more | every seat |
| `/mute here\|<surface> <chat_id> <duration>` | Silence a room, or mute a member for a while | every seat |
| `/ban <duration> IN REPLY to a message` | Ban a member for a while | Telegram, terminal, console |
| `/unban IN REPLY to a message` | Lift a member's ban | Telegram, terminal, console |
| `/unmute IN REPLY to a message` | End a member's mute early | Telegram, terminal, console |
| `/config [list \| get\|explain <key> \| search <words> \| set <key> <value> \| check]` | Read or set preferences | every seat, not in a room |
| `/workers [show\|approve\|remove\|stop\|steer] …` | Named helpers: approve them, and stop or steer a running one | terminal |
| `/prefs [all]` (`/settings`) | The preferences you have set | every seat, not in a room |
| `/mcp [add <id> <url>\|remove <id>\|test <id>]` | MCP servers I can use: add, remove, or test | every seat, not in a room |
| `/model [<name> [default] [reasoning=<level>] \| reset \| aliases]` | The model this chat runs on — switch it live, set your default, pick reasoning | every seat, not in a room |
| `/auth` | The provider keys I have, and how to connect one | terminal |
| `/persona [name-or-text]` | The personas, and the one new chats use | terminal |
| `/profile` | Which agent this terminal runs, and where its files live | terminal |
| `/toolset [name]` | The ability sets, and the one new chats use | terminal |
| `/x` | My X account: renew its login, or see its state | every seat, `x` pack |

### Display

| Command | What it does | Runs on |
|---|---|---|
| `/avatar [show \| set <file\|url> \| set nft <chain>:<contract>:<id>]` | This instance's avatar image — show, set, clear | every seat |
| `/identity register\|set-uri [go]` | My on-chain identity: register it, or update where it is published (spends gas) | every seat, not in a room |
| `/brief` (`/quiet`) | Hide or show the step transcript | terminal |
| `/verbose` | Show or hide the live trace | terminal |

### Leave

| Command | What it does | Runs on |
|---|---|---|
| `/exit` (`/quit`, `/q`) | Leave the terminal | terminal |
| `/help [verb]` (`/h`, `/?`) | This help, or the detail for one verb | every seat |

<!-- END GENERATED chat-commands -->

---

## How some commands work

**Deciding and approving.** `/inbox` is everything waiting on a decision from
you. `/approve <id>` and `/reject <id>` decide one of the items that `/pending`
lists — the same decider on every seat, over the same list. Approval *policy*
(which actions need your approval at all) is `/gates`. When the terminal asks
you to approve an action, answer with a slash token: `/once`, `/session`,
`/always`, `/deny` or `/never`. Plain text is chat, not an answer.

**Money commands quote first.** `/send`, `/swap`, `/bridge`, `/deploy`,
`/launch`, `/lp`, `/claim` and `/nft` only quote until you add `go`. A quote is
an action card: on Telegram you tap its button; on any seat you can type its
token (`/card_<id>_ok`), which does the same. A confirmed card executes exactly
that quote, at most the quote + 5%. Your own command passes the autonomous
ceiling and the pause; the per-transaction and daily caps still apply. The money
model: [payments.md](payments.md).

**Goals and jobs.** `/goal` acts on one goal; `/goals` shows the board. `/job`
acts on one scheduled job; `/jobs` lists them. `/goal allow <id>` re-arms a
title you stopped with `--forever`. Background work:
[streams.md](streams.md).

**Next-session settings.** `/toolset` and `/persona` set the default for the
**next** session; they do not change the tools or the voice of the session you
are in. `/model` changes the model of the running session at once; add
`default` to keep it for new sessions too.

**Pause and stop.** `/pause` stops autonomous work (all of it, or one word:
`trading`, `background`, `messages`, `deploying`); `/halt` is `/pause` with no
words; `/resume` lifts it. `/cancel` stops the turn that runs now. `/run` acts on
one background run. See [owner-controls.md](owner-controls.md).
