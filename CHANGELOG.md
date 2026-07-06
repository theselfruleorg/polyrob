# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
