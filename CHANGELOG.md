# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

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
