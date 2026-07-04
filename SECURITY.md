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

- `AGENT_WALLET_ENABLED=true` — enables the agent's personal wallet.
- `X402_CLIENT_ENABLED=true` — enables the agent-side x402 paying tool.

> ⚠️ **No security audit.** The crypto, wallet, signing, and payment code in POLYROB has
> **not** undergone any independent or third-party security audit. It is provided **as-is**,
> with no warranty (see [LICENSE](LICENSE)), and may contain bugs that lead to loss of funds.
> **Use at your own risk.** Do not entrust it with more value than you are prepared to lose,
> and prefer testnets while evaluating.

**Mainnet use carries real financial risk.** The local implementation has known limitations
documented in the codebase; read the inline notes before enabling mainnet. Never commit private
keys or mnemonics; use environment variables or a secrets manager.

### Code-execution backends

POLYROB ships two code-execution backends, selected via `CODE_EXEC_BACKEND`. The whole
feature is **disabled by default** (`CODE_EXEC_ENABLED=false`).

> **Warning:** The default `local_subprocess` backend is a convenience for single-user /
> local development. It is **NOT a security sandbox** — running it in a multi-tenant or
> production environment is unsafe. For server deployments use the hardened
> `CODE_EXEC_BACKEND=docker` backend (locked-down container: capabilities dropped,
> no-new-privileges, read-only rootfs, workspace-only mount, network deny-by-default —
> see `tools/code_exec/SANDBOX_SECURITY.md`), or keep `CODE_EXEC_ENABLED=false`.

### MCP / external tool integrations

MCP server credentials use `${VAR}` placeholder syntax in `config/mcp_config.json` and are
resolved from environment variables at startup. Never commit real tokens to that file.

### General guidance

- Store all secrets (API keys, wallet keys, tokens) in environment variables or a secrets
  manager — never in source code or config files committed to version control.
- Run POLYROB behind a reverse proxy (nginx/Caddy) with TLS in production.
- The default API key format (`rob_...`) provides a simple authentication layer; for
  production, pair it with network-level controls.
