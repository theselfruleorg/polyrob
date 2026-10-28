# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

Preferred: report vulnerabilities through **GitHub's private vulnerability reporting** — open the
**Security** tab on [github.com/theselfruleorg/polyrob](https://github.com/theselfruleorg/polyrob)
and click **"Report a vulnerability"** (GitHub Security Advisories). This keeps the report
private until a fix is coordinated, and is the canonical reporting channel.

Alternatively, email **info@theselfrule.org**.

Please do not discuss unpatched vulnerabilities on public issues, PRs, or chat — use the private
advisory flow or email only.

Include with your report:

- A clear description of the vulnerability and the affected component.
- Steps to reproduce (proof-of-concept code, screenshots, or a minimal config that triggers
  the issue).
- Your assessment of impact and any suggestions for remediation.

We aim to acknowledge every report within **72 hours** and to provide an initial triage
assessment within 7 days. We will coordinate a fix and a disclosure timeline with you.

We follow responsible disclosure: please give us reasonable time to address the issue before
publishing details publicly.

---

## Supported versions

We provide security fixes for the **latest released version** of POLYROB. Run `polyrob --version`
to check what you're running. Older versions are not backported unless the severity is critical
and the fix is straightforward.

---

## Security posture and important defaults

The full list of environment flags and their defaults is the SSOT at
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md); this section highlights the ones with real
security or financial impact.

### Crypto / wallet / payment features

POLYROB includes optional support for crypto wallets and x402 pay-per-request payments.
These features are **OFF by default** and gated behind environment flags:

- `AGENT_WALLET_ENABLED=true` — enables the agent's personal wallet (`AGENT_WALLET_NETWORK`
  defaults to `testnet`).
- `X402_CLIENT_ENABLED=true` — enables the agent-side x402 paying tool.
- `DEFI_TRADE_ENABLED=true` / `SOLANA_TRADE_ENABLED=true` — enable the on-chain money verbs
  (transfers, approvals, swaps). Every live call is simulated and its measured deltas are held
  to the declared intent, and it refuses unless that chain's RPC is pinned.

Every spend sits inside a per-transaction ceiling (`AGENT_WALLET_MAX_PER_TX_USD`) and a rolling
24-hour cap (`WALLET_DAILY_CAP_USD`); a spend the agent starts above `DEFI_AUTONOMOUS_MAX_USD`
waits for an owner approval. An optional separate signer process (`WALLET_SIGNER=shadow|remote`)
runs under its own user with its own hard caps, which bound every other cap. A wallet seed that
sits in an `.env` file the agent's user can read refuses code runs on the host.

> ⚠️ **No security audit.** The crypto, wallet, signing, and payment code in POLYROB has
> **not** undergone any independent or third-party security audit. It is provided **as-is**,
> with no warranty (see [LICENSE](LICENSE)), and may contain bugs that lead to loss of funds.
> **Use at your own risk.** Do not entrust it with more value than you are prepared to lose,
> and prefer testnets while evaluating.

**Mainnet use carries real financial risk.** The local implementation has known limitations
documented in the codebase; read the inline notes before enabling mainnet. Never commit private
keys or mnemonics; use environment variables or a secrets manager.

### Code-execution backends

Code execution is **disabled by default** (`CODE_EXEC_ENABLED=false`). `CODE_EXEC_BACKEND`
selects the backend:

- `local_subprocess` (the default) — runs on this host. It is **NOT a security sandbox**: use it
  only for single-user local development. A server refuses it.
- `docker` — the hardened container (capabilities dropped, no-new-privileges, read-only rootfs,
  workspace-only mount, network deny-by-default — see `tools/code_exec/SANDBOX_SECURITY.md`).
  `CODE_EXEC_DOCKER_BINARY=podman` runs it on Podman. Use this on a server.
- `ssh` — runs on a remote host with the SSH user's full rights. It is **not a sandbox**; a server
  refuses it unless you set `CODE_EXEC_SSH_SANDBOXED=true` to attest that the host is disposable.
- `modal`, `daytona`, `vercel_sandbox`, `singularity` — optional packs, tested against fakes so far.

The agent's `shell` and `process` tools follow the same rule: they run in the session sandbox,
and only the owner's own turn at the owner's own terminal runs on the host
(`AGENT_COMPUTE_POSTURE=3`). A command guard classifies every shell line: a floor that no approval
lifts, and a dangerous class that waits for the owner and that unattended runs refuse.

### MCP / external tool integrations

MCP server credentials go in `~/.polyrob/mcp.json` (the operator file, outside the code tree),
with `${VAR}` placeholder syntax resolved from environment variables at startup. The tracked
`config/mcp_config.json` holds only the shipped defaults: never put a real token there.

### General guidance

- Store all secrets (API keys, wallet keys, tokens) in environment variables or a secrets
  manager — never in source code or config files committed to version control.
- Run POLYROB behind a reverse proxy (nginx/Caddy) with TLS in production.
- The default API key format (`rob_...`) provides a simple authentication layer; for
  production, pair it with network-level controls.
