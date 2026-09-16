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
| **Language** | Python | Python | TypeScript / Node (native macOS/iOS in Swift) |
| **Primary focus** | Durable, self-hosted autonomy that scales to multi-tenant | Self-improving single-operator agent | Omni-channel personal assistant |
| **Multi-provider LLM** | ✅ [Six built-ins plus flat-rate rows, OAuth plans and your own endpoint](guide/configuration.md#3-providers-and-models) | ✅ 300+ models (portal / OpenRouter / own endpoint) | ✅ Claude, OpenAI, Gemini, DeepSeek |
| **Provider failover** | ✅ Automatic cross-provider on billing, quota and rate-limit errors | ✅ Credential pooling + rotation | ✅ Across auth profiles |
| **Subscription sign-in (OAuth)** | ✅ Claude Pro/Max, ChatGPT Codex, Copilot, SuperGrok, Qwen, MiniMax | ❓ | ✅ ChatGPT / Codex |
| **Persistent memory** | ✅ SQLite FTS5, optional local vector recall, tenant-scoped | ✅ FTS5 + reflective/curated, pluggable providers | ⚠️ Session history + workspace files |
| **Durable task/goal board** | ✅ Goal board + objectives + cron, all restart-surviving | ✅ Kanban board with DAG decomposition | ❌ (cron + sessions only) |
| **Proactive self-wake** | ✅ Self-wake with depth/backoff guards and a no-change gate | ❌ | ❌ |
| **Skills system** | ✅ `SKILL.md` + install pipeline (local/git/URL, scanned and quarantined) | ✅ Autonomous skill creation and learning loop | ✅ Workspace skills |
| **Messaging channels** | 7 — Telegram, WhatsApp, Email, Discord, Slack, Signal, X | ~30 (adds WeChat, Feishu, LINE, Matrix, iMessage, …) | ~23 + native mobile/desktop apps |
| **Group rooms** | ✅ Allow-listed rooms with a read-only room toolset, per-room policy, roles and caps | ❓ | ❓ |
| **REST API** | ✅ Built-in, documented | ❌ (gateway / web, not a documented REST session API) | ❌ |
| **A2A protocol** | ✅ Google spec, JSON-RPC + REST + SSE | ❌ (exposes ACP to editors) | ❌ |
| **MCP client** | ✅ | ✅ | ✅ |
| **MCP server (inbound)** | ✅ `POST /mcp`, 5 read-only tenant-scoped tools | ✅ (`mcp_serve.py`, not headlined) | ❌ (community request) |
| **Delegation / multi-agent** | ✅ Least-privilege sub-agents | ✅ RPC + kanban swarm | ✅ Multi-agent routing |
| **Wallet and payments** | ✅ Agent wallet, x402 in and out, invoicing, spend caps | ❌ | ❌ |
| **On-chain capability** | ✅ Swaps on EVM + Solana, cross-chain bridging, token deployment, launchpads, and a wallet inside the browser for any dapp | ❌ | ❌ |
| **Durable app service** | ✅ The agent's own app runs past the session behind a public URL, owner-approved | ❓ | ❓ |
| **Portable identities** | ✅ Profiles: a whole isolated home, exportable and git-installable | ❓ | ❓ |
| **Tenant isolation** | ✅ Multi-tenant, `user_id`-scoped inside one process | ❌ Single-operator | ❌ Single-operator |
| **Access model** | ✅ 4 tiers — owner / correspondent / group member / denied — plus origin taint | ⚠️ Binary authorized-or-not + DM pairing | ⚠️ DM pairing + allowlists |
| **Owner-approval queue** | ✅ Durable and remotely approvable | ⚠️ In-memory, lost on restart | ⚠️ Pairing gates channel access, not per-tool |
| **Code-exec sandboxing** | ✅ Docker (opt-in); local and ssh backends | ✅ 6 backends (local, Docker, SSH, Singularity, Modal, Daytona) | ✅ Docker / SSH / OpenShell |
| **Self-hosted** | ✅ | ✅ | ✅ |

> **Licensing is table stakes, not a differentiator** — all three are MIT. POLYROB
> ships the full engine under MIT, self-hosted: free forever, yours to fork and run,
> with no limited "open core" held back behind a paid cloud. The POLYROB and Selfrule
> *names* are trademarks ([TRADEMARK.md](../TRADEMARK.md)) — fork the code freely,
> just rename your distribution.

---

## What sets POLYROB apart

Two of the three projects here are larger and more established. POLYROB does not
compete on channel breadth or community size. These are the capabilities neither
upstream has:

- **Economic agency.** A built-in agent wallet, x402 pay-per-request in both
  directions, invoicing with branded QR cards, on-chain settlement detection, and
  rolling spend caps that no autonomy setting can widen. On top of that: swaps on
  EVM and Solana, cross-chain bridging proved by measured arrival, fixed-supply
  token deployment, launchpads, and an injected wallet that makes any web dapp
  usable. Neither Hermes nor OpenClaw has a payment or wallet capability at all.
- **A four-tier access model with origin taint.** Every inbound message resolves to
  **owner** (steers the agent), **correspondent** (a third party the agent contacted
  first — its replies enter as *data*, never as instructions), **group member** (a
  room participant, answered from a read-only room toolset), or **denied**. While a
  session is correspondent-tainted, high-impact tools are gated off. Both upstreams
  are single-operator; Hermes' own security docs recommend separate instances for
  isolation.
- **Proactive self-wake.** POLYROB can re-enter an idle session on its own when
  observable state changes, with depth and backoff guards and a change gate so a
  no-op wake costs nothing. Both upstreams are reactive — proactivity comes from
  cron, a queue, or a completion callback.
- **A durable, remotely approvable owner queue.** High-impact and money actions wait
  in a persisted queue that survives a restart and can be approved from a phone.
  Hermes' approval is pattern-based and in-memory; OpenClaw's pairing gates channel
  access rather than individual actions.
- **Work that outlives the session.** An app the agent builds can be deployed behind
  a public URL and kept alive by an owner-run supervisor — the agent writes the
  registry row, the owner approves the address, and the privileged half (containers,
  nginx, egress rules) is never the agent's to hold.
- **Portable identities.** A profile is a whole isolated home — env, characters,
  skills, memory, goals, sessions — that you can export, or install from a git repo.
  Credentials and the agent's own memories never travel with one.

Where POLYROB **lags**, honestly: **channel breadth** (7 against Hermes' ~30 and
OpenClaw's ~23, and OpenClaw's native mobile and desktop apps), **code-exec backend
variety** (Hermes offers serverless backends such as Modal and Daytona), and
**community size** (POLYROB is the newer project).

> POLYROB's tenant isolation is enforced by `user_id`-scoped checks inside one shared
> process — real and load-bearing, but a software boundary, not an operating-system
> one. For genuinely adversarial multi-tenancy, run separate instances. See
> [security-model.md](guide/security-model.md).

---

## Choosing

| Choose | When you need |
|---|---|
| **POLYROB** | An agent that **transacts** — quote, invoice, get paid, pay for resources, trade and deploy on-chain · **more than one person** talking to it, with a stranger's input treated as data · **durable proactive autonomy** that survives restarts · **restart-safe approvals** you can grant from your phone · **interoperability**: MCP client and server, A2A, REST and an OpenAI-compatible surface · self-hosted control of your keys and data |
| **Hermes** | The **widest channel and model reach** for a single-user setup · a **skill learning loop** · a **kanban task swarm** with DAG decomposition · **serverless sandboxes** (Modal, Daytona) |
| **OpenClaw** | **Omni-channel presence** across ~23 platforms · **native mobile and desktop apps** and voice mode · a **Node.js/TypeScript** stack |

---

## Migrating

- [from-hermes.md](guide/migration/from-hermes.md)
- [from-openclaw.md](guide/migration/from-openclaw.md)
- [guide/upgrading.md](guide/upgrading.md) — moving between POLYROB versions

---

## Appendix — competitor snapshot

**Verified 2026-07-23** against each project's live upstream repository: Hermes at
its v0.19.0 release (2026-07-20), OpenClaw on `main`. Anything below this line is a
point-in-time observation about someone else's project, not a durable claim.

### Hermes Agent

Best for a single operator who wants a self-improving agent with broad channel
reach and a large model catalog.

- ~30 connectors, including WeChat, Feishu, LINE, Matrix and iMessage.
- 300+ models through Nous Portal, OpenRouter or your own endpoint, with
  credential pooling and rotation.
- Autonomous skill creation; skills self-improve during use.
- A kanban swarm: a multi-worker task board with DAG decomposition.
- Six code-exec backends, including the idle-hibernating Modal and Daytona.
- Ships an `mcp_serve.py` MCP-server surface, not foregrounded in its README.
- Trade-offs at that date: single-operator (authorization is binary), no wallet or
  payments, no self-wake primitive, and in-flight approvals lost on restart.

### OpenClaw

Best for an omni-channel personal assistant with first-class native apps.
(Formerly Warelay → CLAWDIS → Clawdbot → Moltbot → OpenClaw.)

- ~23 messaging platforms, including WhatsApp, iMessage, Matrix, Feishu and WeChat.
- Native macOS app and iOS/Android nodes; wake words and continuous voice.
- Subscription-auth providers (ChatGPT/Codex OAuth) with failover across auth
  profiles.
- Docker (default), SSH and OpenShell sandboxes with per-mode tool allow/deny lists.
- Trade-offs at that date: single-operator (pairing gates channel access), no wallet
  or payments, no provider-agnostic REST API, no dedicated vector recall, and MCP
  client only.
