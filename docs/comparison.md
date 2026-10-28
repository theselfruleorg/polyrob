# POLYROB vs other agent frameworks

How POLYROB compares with two other prominent open-source agent projects,
**Hermes** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
and **OpenClaw** ([openclaw/openclaw](https://github.com/openclaw/openclaw)).

The POLYROB column describes what this repository does now. The Hermes and
OpenClaw columns are a **snapshot** — both are fast-moving projects, so check
their repos for anything load-bearing to your decision, and read the dated
appendix at the end for when each claim was verified.

**Legend:** ✅ supported · ⚠️ partial or with caveats · ❌ not supported ·
❓ we could not verify it on the live repo, so we did not guess.

---

## At a glance

| | POLYROB | Hermes | OpenClaw |
|---|---|---|---|
| **License** | MIT | MIT | MIT |
| **Language** | Python | Python (desktop app on Electron) | TypeScript / Node, plus native companion apps |
| **Primary focus** | Durable, self-hosted autonomy that scales to multi-tenant | Self-improving agent with a learning loop, for one operator | Personal assistant across channels and devices; one Gateway can also serve a trusted team |
| **Multi-provider LLM** | ✅ [Six built-ins plus flat-rate rows, OAuth plans and your own endpoint](guide/configuration.md#4-providers-and-models) | ✅ Nous Portal (300+ models), OpenRouter, dozens of native providers | ✅ ~80 provider plugins |
| **Local models** | ✅ Any OpenAI- or Anthropic-compatible endpoint in [`providers.yaml`](guide/configuration.md#your-own-endpoint-providersyaml) (Ollama, LM Studio, vLLM, llama.cpp) | ✅ One-click llama.cpp runtime that picks a model build to fit your hardware | ✅ Ollama, LM Studio, vLLM and SGLang provider plugins |
| **Provider failover** | ✅ Automatic cross-provider on billing, quota and rate-limit errors | ✅ Credential pools plus a cross-provider fallback chain | ✅ Auth-profile rotation plus a model fallback chain |
| **Subscription sign-in (OAuth)** | ✅ Claude Pro/Max, ChatGPT Codex, Copilot, SuperGrok, Qwen, MiniMax | ✅ Nous Portal, ChatGPT/Codex, Claude Max (extra credits only), SuperGrok, Qwen, MiniMax | ✅ ChatGPT/Codex OAuth, Claude CLI reuse |
| **Persistent memory** | ✅ SQLite FTS5, optional local vector recall, tenant-scoped | ✅ Curated `MEMORY.md`/`USER.md`, FTS5 session search, Honcho user modelling, memory-provider plugins | ✅ Markdown memory files with hybrid embedding and keyword search |
| **Durable task/goal board** | ✅ Goal board + scheduled jobs, all restart-surviving, every autonomous run under one step cap and one spend cap | ✅ Kanban board across profiles, plus a judge-checked `/goal` loop | ⚠️ Opt-in Workboard plugin (cards with dependency promotion and worker dispatch), restart-surviving session goals, scheduled automations |
| **Proactive self-wake** | ✅ Wakes an idle session when observable state changes, with depth/backoff guards and a no-change gate | ✅ Timer-driven: `/heartbeat` and `/loop` re-enter an idle session; cron monitor jobs skip the model when nothing changed | ⚠️ Scheduled automations wake a session; standing intents fire when a matching event appears |
| **Skills system** | ✅ `SKILL.md` + install pipeline (local/git/URL, scanned and quarantined) | ✅ Autonomous skill creation, a curator, the Skills Hub | ✅ AgentSkills format, the ClawHub registry, a skill editor in the Control UI |
| **Plugins / extensions** | ⚠️ Packs: seven first-party (X, markets, discovery, four sandbox backends); a third-party pack loads only when you name it | ✅ A reviewed plugin catalog (600 entries at v0.22.0, each pinned to a commit) | ✅ Official and external plugins, plus ClawHub |
| **Messaging channels** | 8 — Telegram, WhatsApp, Email, Discord, Slack, Signal, Feishu / Lark, DingTalk — plus X DMs through the agent's X tools | ~29 built-in adapters (adds Matrix, Teams, iMessage, WeChat, LINE, SMS, …) plus platform plugins | ~33 (adds iMessage, Teams, Matrix, LINE, Nostr, X, Twitch, …) plus WebChat |
| **Native apps and voice** | ⚠️ Web console and terminal only; voice notes on Telegram and WhatsApp are transcribed, and Telegram can answer with a voice note | ✅ Desktop app (signed on macOS and Windows), Android through Termux; streaming voice with barge-in and wake words | ✅ macOS, iOS, Android, Windows and Linux companion apps; Talk mode, voice wake, Canvas |
| **Group rooms** | ✅ Allow-listed rooms with a read-only room toolset, per-room policy, roles and caps | ⚠️ Group/channel scopes with their own admin list and allowed commands; multi-bot rooms in the desktop app | ✅ Allow-listed groups, mention gating, ambient room events; group sessions can run sandboxed |
| **REST API** | ✅ Built-in, documented | ✅ OpenAI-compatible API server, plus Runs and Jobs APIs | ⚠️ OpenAI-compatible Chat Completions and OpenResponses endpoints, off by default; a token is full operator access |
| **A2A protocol** | ✅ A2A 1.0, JSON-RPC + REST + SSE | ✅ A2A 1.0 plugin, both directions | ✅ A2A 1.0 bundled channel plugin, both directions |
| **Editor integration (ACP)** | ❌ | ✅ `hermes acp` | ✅ `openclaw acp` |
| **MCP client** | ✅ | ✅ | ✅ |
| **MCP server (inbound)** | ✅ `POST /mcp`, 5 read-only tenant-scoped tools | ✅ The `mcp-serve` plugin | ✅ `openclaw mcp serve`, a stdio bridge to channel conversations |
| **Delegation / multi-agent** | ✅ Least-privilege sub-agents | ✅ Subagents you can steer live, Kanban workers, bot-to-bot DMs (`hermes peer`) | ✅ Multi-agent routing and subagents |
| **Wallet and payments** | ✅ Agent wallet, x402 in and out, invoicing, spend caps, an optional separate signer process | ⚠️ Not built in: optional MPP (HTTP 402) and Stripe Link skills; community wallet plugins in the catalog | ❌ Not in the repository |
| **On-chain capability** | ✅ Swaps on EVM + Solana, cross-chain bridging, token deployment, launchpads, a wallet inside the browser for any dapp, and an owner-set trading mandate with code-armed exits | ⚠️ Read-only EVM, Solana and Hyperliquid skills (optional); community plugins add transfers and swaps | ❌ |
| **Durable app service** | ✅ The agent's own app runs past the session behind a public URL, owner-approved | ⚠️ No supervised app service; an optional Pinggy skill opens a temporary localhost tunnel, and the desktop app previews generated apps in a sandbox | ⚠️ Portals show an agent-run dev server to the operator only, through the Gateway; not a public URL |
| **Portable identities** | ✅ Profiles: a whole isolated home, exportable and git-installable | ✅ Profiles: export files and git-installed "profile distributions" | ⚠️ `openclaw backup` archives and restores state; Claws are portable agent setups (instructions, skills, plugins, automations) with no export of a running agent |
| **Import from another agent** | ❌ No importer — the [migration guides](#migrating) are manual | ✅ `hermes import-agent` (OpenClaw, Claude Code, Codex, Cursor) | ✅ `openclaw migrate` (Hermes, Claude, Codex) |
| **Tenant isolation** | ✅ Multi-tenant, `user_id`-scoped inside one process | ❌ Single-operator: one agent per home, one memory per profile | ❌ One trust domain per Gateway; its multi-user mode is "not a security boundary" |
| **Access model** | ✅ 4 tiers — owner / correspondent / group member / denied — plus origin taint | ⚠️ Allowlists + DM pairing + an admin/user split that gates slash commands only | ⚠️ DM pairing, allowlists, owner-only tools, named operator roles |
| **Terminal / shell** | ✅ Persistent shell + background jobs (stdin, PTY, notify); sandboxed by default, host only for the owner's own terminal turn; a command guard with a floor no approval lifts | ✅ | ✅ |
| **Owner-approval queue** | ✅ Durable and remotely approvable; one-tap action cards, and the agent continues once every card is decided | ⚠️ An in-process wait with a fail-closed timeout (300 s default); not restart-durable | ⚠️ Per-command exec approvals answerable from chat, the apps or the CLI; a pending request dies with its turn |
| **Code-exec sandboxing** | ✅ Docker or Podman (opt-in); local and ssh backends; Modal, Daytona, Vercel Sandbox and Singularity as optional packs ⚠️ (tested against fakes so far) | ✅ 6 built-in backends (local, Docker, SSH, Modal, Daytona, Vercel Sandbox) plus a Singularity plugin | ✅ Docker, Podman, SSH, OpenShell and Crabbox backends; off by default |
| **Self-hosted** | ✅ | ✅ | ✅ |

> **Licensing is table stakes, not a differentiator** — all three are MIT. POLYROB
> ships the full engine under MIT, self-hosted: free forever, yours to fork and run,
> with no limited "open core" held back behind a paid cloud. The POLYROB and Selfrule
> *names* are trademarks ([TRADEMARK.md](../TRADEMARK.md)) — fork the code freely,
> just rename your distribution.

---

## What sets POLYROB apart

Both other projects are larger and more established, and both shipped a great deal
between July and October 2026: Hermes added A2A, an OpenAI-compatible API server,
timer-driven heartbeats, shareable profiles and a plugin catalog; OpenClaw added
A2A, an MCP server, vector memory search, session goals and team roles. POLYROB
does not compete on channel breadth, native apps or community size. These are the
capabilities neither upstream has built in:

- **Economic agency.** A built-in agent wallet, x402 pay-per-request in both
  directions, invoicing with branded QR cards, on-chain settlement detection, and
  rolling spend caps that no autonomy setting can widen. On top of that: swaps on
  EVM and Solana, cross-chain bridging proved by measured arrival, fixed-supply
  token deployment, launchpads, and an injected wallet that makes any web dapp
  usable. OpenClaw has no payment or wallet capability; Hermes reaches payments
  only through optional MPP and Stripe Link skills, read-only chain skills and
  community wallet plugins — none of them under a core spend cap.
- **A four-tier access model with origin taint.** Every inbound message resolves to
  **owner** (steers the agent), **correspondent** (a third party the agent contacted
  first — its replies enter as *data*, never as instructions), **group member** (a
  room participant, answered from a read-only room toolset), or **denied**. While a
  session is correspondent-tainted, high-impact tools are gated off. Hermes'
  admin/user split gates slash commands only; OpenClaw's operator roles and
  owner-only tools sit inside one trust domain, and its own docs call multi-user
  mode "not a security boundary".
- **Tenant isolation.** Several tenants share one process with `user_id`-scoped
  memory, goals and sessions. Hermes keeps one agent per home; OpenClaw tells you to
  run separate Gateways when people must not reach each other's data.
- **A durable, remotely approvable owner queue.** High-impact and money actions wait
  in a persisted queue that survives a restart and can be approved from a phone.
  Hermes' approval is an in-process wait with a timeout; OpenClaw's pending exec
  approval belongs to the live turn and dies with it.
- **Change-driven self-wake.** POLYROB re-enters an idle session when observable
  state changes, with depth and backoff guards and a change gate so a no-op wake
  costs nothing. Hermes now has timer-driven `/heartbeat` and `/loop`, and OpenClaw
  has scheduled automations — both wake on a clock, not on a change.
- **Work that outlives the session.** An app the agent builds can be deployed behind
  a public URL and kept alive by an owner-run supervisor — the agent writes the
  registry row, the owner approves the address, and the privileged half (containers,
  nginx, egress rules) is never the agent's to hold.

Portable identities are **not** on this list any more: Hermes' profiles now export to
a file and install from git as well.

Where POLYROB **lags**, honestly:

- **Channel breadth** — 8 against Hermes' ~29 and OpenClaw's ~33.
- **Native apps and voice** — both upstreams ship desktop and mobile apps, wake
  words and live voice; POLYROB has a web console, a terminal and voice notes.
- **Extension ecosystem** — Hermes' reviewed catalog lists hundreds of plugins and
  OpenClaw has ClawHub; POLYROB has seven first-party packs.
- **Local models in one click** — Hermes installs and sizes a llama.cpp runtime for
  you; POLYROB reaches a local server you run yourself.
- **Editor integration and importers** — both upstreams speak ACP to editors and
  import each other's setups; POLYROB does neither.
- **Maturity of its serverless sandboxes** — the Modal, Daytona, Vercel Sandbox and
  Singularity backends ship as optional packs but have been tested against fakes,
  not run at scale.
- **Community size** — POLYROB is the newer project.

> POLYROB's tenant isolation is enforced by `user_id`-scoped checks inside one shared
> process — real and load-bearing, but a software boundary, not an operating-system
> one. For genuinely adversarial multi-tenancy, run separate instances. See
> [security-model.md](guide/security-model.md).

---

## Choosing

| Choose | When you need |
|---|---|
| **POLYROB** | An agent that **transacts** — quote, invoice, get paid, pay for resources, trade and deploy on-chain · **more than one person** talking to it, with a stranger's input treated as data · **durable proactive autonomy** that survives restarts · **restart-safe approvals** you can grant from your phone · **interoperability**: MCP client and server, A2A, REST and an OpenAI-compatible surface · self-hosted control of your keys and data |
| **Hermes** | The **widest model reach** (Nous Portal, one-click local models) for a single operator · a **skill learning loop** · a **Kanban board** across profiles · a **desktop app** and voice · a large **plugin catalog** · **proven serverless sandboxes** (Modal, Daytona, Vercel Sandbox) |
| **OpenClaw** | **Omni-channel presence** across ~33 platforms · **native mobile and desktop apps**, voice wake and Canvas · a **trusted-team Gateway** with operator roles and shared sessions · a **Node.js/TypeScript** stack |

---

## Migrating

- [from-hermes.md](guide/migration/from-hermes.md)
- [from-openclaw.md](guide/migration/from-openclaw.md)
- [guide/upgrading.md](guide/upgrading.md) — moving between POLYROB versions

---

## Appendix — competitor snapshot

**Verified 2026-10-27** against each project's live upstream repository and the
documentation source in that repository. Anything below this line is a
point-in-time observation about someone else's project, not a durable claim.

| Project | Release at that date | Docs and source read at |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | v0.22.5 (2026-10-23) | `main` @ `11a7535` (2026-10-27), `website/docs/` = [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/) |
| [OpenClaw](https://github.com/openclaw/openclaw) | 2026.10.8 (2026-10-27) | `main` @ `fd3dfa2` (2026-10-27), `docs/` = [docs.openclaw.ai](https://docs.openclaw.ai) |

The previous snapshot (2026-07-23) read Hermes v0.19.0 and OpenClaw `main`.

### Hermes Agent

Best for a single operator who wants a self-improving agent with broad model and
channel reach, a desktop app and a large plugin catalog.

- Since v0.19.0: v0.20.0 (2026-08-03) added streaming voice with wake words,
  outbound signed webhooks, desktop artifacts and an A2A v1.0 plugin; v0.21.0
  (2026-08-31) added Bot Mode (multi-bot rooms), `hermes peer` bot-to-bot DMs,
  cron jobs with memory and a no-change monitor mode, and live subagent steering;
  v0.22.0 (2026-10-13) added hosted-app connectors, one-click local models, signed
  desktop packages and stable release channels, a 600-entry plugin catalog,
  password-manager sign-in for the browser, and one gateway for every profile.
- ~29 built-in messaging adapters (Telegram, Discord, Slack, WhatsApp, Signal, SMS,
  Email, Matrix, Mattermost, Teams, Google Chat, Feishu/Lark, DingTalk, WeCom,
  Weixin, QQ, LINE, iMessage through BlueBubbles or Photon, IRC, SimpleX, …).
- 300+ models through Nous Portal, plus OpenRouter, native providers and OAuth
  sign-in to several consumer plans; credential pools and a fallback chain.
- An OpenAI-compatible API server with a Runs API (approvals answerable over HTTP)
  and a Jobs API; A2A both directions; ACP for editors; MCP client, and MCP server
  through the `mcp-serve` plugin.
- Kanban board, a judge-checked `/goal`, and timer-driven `/heartbeat` and `/loop`.
- Six built-in terminal backends plus a Singularity plugin; a hardline command
  blocklist below every approval mode.
- Payments and chain access are optional skills (MPP, Stripe Link; read-only EVM,
  Solana, Hyperliquid) and community catalog plugins, not core.
- Trade-offs at that date: single-operator (an admin/user split gates slash
  commands only; one agent per home), no built-in wallet, and dangerous-command
  approvals that wait in process with a timeout rather than in a durable queue.

### OpenClaw

Best for an omni-channel personal assistant with first-class native apps, or a
trusted team sharing one Gateway.

- ~33 channels in the official catalog, including iMessage, Teams, Matrix, LINE,
  Nostr, Twitch, X (mentions and allow-listed replies), WeChat and Zalo, plus WebChat.
- Companion apps for macOS, iOS, Android, Windows and Linux; Talk mode, voice wake
  words and Canvas.
- ~80 provider plugins including local servers; ChatGPT/Codex OAuth and Claude CLI
  reuse; auth-profile rotation and a model fallback chain.
- Memory as Markdown files with hybrid embedding and keyword search; restart-surviving
  session goals; an opt-in Workboard (Kanban) plugin; scheduled automations and
  event-conditioned standing intents.
- A2A 1.0 both directions, `openclaw mcp serve`, `openclaw acp`, and an
  OpenAI-compatible endpoint that is off by default and carries full operator access.
- Sandboxing (Docker, Podman, SSH, OpenShell, Crabbox) is off by default; exec
  approvals with standing grants, answerable from chat, the apps or the CLI.
- Multi-user mode and named operator roles for a trusted team, with the stated
  trust boundary of one Gateway.
- Trade-offs at that date: one trust domain per Gateway (no tenant isolation), no
  wallet or payments, and pending approvals bound to the live turn.
