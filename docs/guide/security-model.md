# Security & trust model

This page is the one honest, consolidated answer to "what actually stops the agent
from doing something bad?" Short version: almost every gate described elsewhere in
this repo — correspondent taint, the tool-capability table, approval hooks, money
caps — is a **heuristic running inside the same trusted process as the agent
itself**. They are well-designed and load-bearing, but none of them is a hard
security boundary. The only hard boundary is the **operating system / container**,
and this page names exactly where that boundary exists today, where it doesn't, and
what to do about it depending on how you're running polyrob.

> This page complements, and deliberately does not duplicate:
> **[SECURITY.md](../../SECURITY.md)** (vulnerability reporting, the unaudited-crypto
> disclaimer), **[`tools/code_exec/SANDBOX_SECURITY.md`](../../tools/code_exec/SANDBOX_SECURITY.md)**
> (the Docker code-exec backend's hardening in full detail), and
> **[deployment-postures.md](deployment-postures.md)** (the web console's `local` /
> `own_ops` / `multitenant` posture ladder).

---

## 1. Threat model — who and what is untrusted

An agent session is driven by content from several sources, only one of which is
actually trusted:

| Source | Trust level | Why |
|---|---|---|
| The **owner** (bound owner principal, or the local CLI operator) | Trusted | Steers the agent directly — COMMAND/STEER/TASK_AGENT access |
| A **correspondent** (a third party the agent itself initiated contact with) | Untrusted DATA | Their reply is delivered to the session as framed data, never as a command — see §2 |
| **Fetched web content / browser page content / MCP tool outputs** | Untrusted DATA | Anything from `browser`, `web_fetch`, `mcp`, `perplexity`, `anysite`, `email` bodies, etc. may contain adversarial text (indirect prompt injection) |
| **Skill content** authored by the agent itself (writable skills) | Untrusted-by-default | Content is threat-scanned before write; a forged/background turn can never auto-activate or patch an active skill |
| An **unknown or unverified sender** on any surface | Denied | No routing authority — see the OWNER/CORRESPONDENT/DENIED tiers below |

The dispatcher (`core/surfaces/dispatcher.py::route_inbound`) resolves exactly one
of three tiers per inbound message, fail-closed once the access model is on
(`CORRESPONDENT_ACCESS_ENABLED`): **OWNER**, **CORRESPONDENT**, or **DENIED**. A
correspondent's reply can *never* reach COMMAND/STEER/TASK_AGENT — it is delivered
as a `MessageOrigin.CORRESPONDENT` control message, framed with an
`<untrusted_tool_result>` wrapper, exactly like a fetched web page.

This page is about the layer *underneath* that model: given that untrusted content
routinely reaches the agent's context (by design — that's how it does useful work),
what actually stops it from *acting* on that content in a dangerous way?

---

## 2. The layered heuristics — and what each one actually stops

All of the following run **in-process**, in the same Python interpreter, as the
same OS user, with the same filesystem and network access as the rest of the
agent. They are correctness/policy layers, not isolation layers.

### Correspondent taint gate (`agents/task/agent/core/correspondent_gate.py`)

A pre-tool-call hook: while the latest input on a session is
correspondent-untrusted, high-impact tools (money, outbound messaging, code
execution, delegation, browser) are denied. This is the **structural backstop**
behind the prompt-level `<untrusted_tool_result>` framing — a forged email can't
directly drive a spend/send/execute action even if it partially succeeds at
prompt injection.

What it actually stops: an agent that has been talked into "just do X" by
untrusted DATA from calling a dangerous *tool*, for the duration of the taint.

What it does **not** stop: anything the untrusted content can achieve *without*
calling a gated tool — e.g. influencing what the agent later tells the *owner*
(social-engineering the human, not the gate), or a bug in the gate's own
resolution logic. The gate's own docstring documents its sharpest edge: the
pre-hook receives the bare *action name*, not the *tool_id* — a denylist keyed on
the wrong one is silently dead. It's implemented correctly today, but that's a
property of the code, not of the mechanism.

### Tool-capability table (`core/tool_capabilities.py`)

The single SSOT for `money` / `high_impact` / `delegate_blocked` / `exec` /
`readable_while_tainted` per tool_id — derives `MONEY_TOOLS`,
`HIGH_IMPACT_TOOL_IDS`, `DELEGATE_BLOCKED_TOOLS`. `register_optional_tool` refuses
to register a tool that isn't classified here, which closes the "new tool
silently skips every gate" failure mode.

What it stops: an *unclassified* tool from ever becoming callable, and a
delegated leaf sub-agent from ever getting a blocked toolset.

What it does not do: enforce anything about a tool's *behavior* once it's
loaded and permitted — the table says "code_execution is exec + delegate_blocked",
it does not itself sandbox what `run_code` does (see §3).

### Approval hooks (`tools/controller/approval.py`)

`ApprovalProvider` + `make_approval_hook`, wired `fail_mode="closed"` so a denial,
timeout, or a crashing provider **blocks** the action rather than allowing it.
`APPROVAL_REQUIRED_TOOLS` (env, frozen at import) plus a `DEFAULT_APPROVAL_REQUIRED_TOOLS`
set the Controller unions in automatically at `AGENT_COMPUTE_POSTURE >= 2`. The
durable `owner_queue` provider makes this remotely-approvable and restart-surviving.

What it stops: a gated action executing without an explicit approval decision,
even across a process restart.

What it does not do: validate that the *approved* action is itself safe — approval
is a human/policy checkpoint, not a sandbox. And it only covers tools someone
remembered to put in `APPROVAL_REQUIRED_TOOLS` (or the posture-derived defaults).

### Money gates (`modules/x402`, `core/wallet/`)

`WALLET_DAILY_CAP_USD`, per-venue caps, `PAYMENT_APPROVAL_MODE` (`approve` default,
routes every outward payment through `owner_queue`; `auto` auto-approves within
caps and notifies after), `X402_INVOICE_MAX_USD` / `X402_INVOICE_DAILY_MAX`, and
the correspondent-taint gate (money tools are always `high_impact`) all compose to
bound how much value the agent can move and under what conditions.

What it stops: unbounded spend, and spend without a paper trail or a cap.

What it does not do: protect the wallet's *key material* from a host-level
compromise — if the process itself is compromised (see §3's "process identity" gap),
the caps are enforced by the same process an attacker would already control.

### The pattern across all four

Every one of these is **software logic evaluated by the same interpreter that
runs the agent loop**. They're real, they're tested, and defeating them requires
either a genuine implementation bug or a novel bypass path — not "jailbreak the
sandbox," because there usually isn't one at this layer. That's exactly why they
need a second, independent line of defense underneath them: the OS/container
boundary.

---

## 3. The honest holes — what runs unsandboxed on the host today

These are not bugs to be quietly worked around; they are documented, deliberate
trade-offs (or, in one case, an unwired-but-designed lever). Naming them precisely
is the point of this page.

### a) `run_code` / `run_tests` on the host under `POLYROB_LOCAL`

`tools/code_exec/sandbox_guard.py::require_sandbox_or_none` / `code_exec_execution_blocked_reason`:

```python
def require_sandbox_or_none(backend_name: str) -> Optional[str]:
    from core.config_policy import local_mode_enabled
    if local_mode_enabled():
        return None          # <- local mode: always allowed, no sandbox check
    ...
```

Under `POLYROB_LOCAL=1` (the CLI's local profile), `run_code`/`run_tests` are
allowed on the `local_subprocess` backend — a **plain host subprocess**, not a
sandbox (`capabilities["sandbox"] is False`) — no matter what `CODE_EXEC_BACKEND`
is set to. This is a deliberate single-user trade for CLI convenience. On a
server (`POLYROB_LOCAL` unset), the same guard **refuses** any backend that
doesn't advertise `capabilities["sandbox"] is True` — so a server is fail-closed
to `docker` (or nothing) by construction. `local_subprocess` on a server is a hard
refusal, not a silent downgrade.

### b) LSP diagnostics and shadow-git snapshots always run on the host

`tools/coding/tool.py` calls `tools.coding.lsp.diagnose_file` (~line 211, gated
only by `CODING_LSP_ENABLED`, default OFF) and `tools.coding.snapshot.snapshot_file`
(~line 274, gated by `CODING_SNAPSHOT_ENABLED` **and**
`compute_posture_allows(ctx, 1)`) to run external checker binaries (pyright/tsc)
and `git` as **host subprocesses** — every time, in every deployment shape,
including a multi-tenant server. Unlike `run_code`/`run_tests`, there is **no
sandbox routing for these at all**: `diagnose_file` has no posture check
whatsoever, and `snapshot_file`'s posture check governs *who* can trigger it
(owner tenant, non-leaf, non-forged turn) — it does not route the subprocess into
any sandbox. Both are fail-open on error (a crash never blocks the edit) and both
default OFF, but "OFF by default" is a flag, not a sandbox: turning either on
anywhere except a fully-trusted single-user box means external binaries execute
with the same host privileges as the whole agent process.

### c) MCP stdio servers inherit the full process environment

`tools/mcp/protocol.py` (~line 349):

```python
full_env = {**os.environ, **(self.env or {})}
self.process = subprocess.Popen(self.command, ..., env=full_env, ...)
```

Every configured MCP server (`config/mcp_config.json`) is spawned as a **full
sibling host process that inherits every environment variable the main agent
process has** — LLM provider keys, DB credentials, whatever else lives in the
process env. There is no allowlist and no scrubbing at this layer. Contrast this
with the `code_execution` tool's backends, which explicitly build a scrubbed,
allowlisted child environment (`tools/code_exec/env_policy.py`) and (for
`DockerBackend`) filter even the *caller-supplied* env through a
`SECRET_PAT`-based deny list. An MCP server is either a fully-trusted process, or
it shouldn't be in `mcp_config.json` at all — there is currently no middle ground.

### d) `AGENT_COMPUTE_POSTURE=3` ("host") is designed but unwired

`core/config_policy/policy.py` defines the posture (0 `confined` / 1 `sandbox-dev`
/ 2 `self-maintain` / 3 `host`) and the single gate predicate
`compute_posture_allows(execution_context, min_posture)`, but **no code path in
the tree currently calls it with `min_posture=3`**. Nothing today grants
host-tier capability through this lever — it is a reserved, not an active,
capability. If a feature is ever built against posture 3, it inherits the
documented requirement: `POLYROB_LOCAL` **and** a single-tenant box, refused on
any network-facing surface.

### e) Process identity in the reference production deployment

The shipped systemd units (`deployment/polyrob.service`,
`polyrob-webview.service`, `polyrob-email.service`) all run `User=root`, with none
of `ProtectSystem`, `ProtectHome`, `NoNewPrivileges`, or `PrivateTmp` set. This
matters directly for everything above: on that exact deployment shape, the
process every in-process gate in §2 lives inside **already has full host root**.
The heuristic layers are, today, the *only* thing standing between a bug in one of
them and full host compromise — not a second, independent wall. (`polyrob-api.service`
/ `polyrob-webgate.service`, the self-hosting OSS posture, already run as
`User=ubuntu` — non-root is achievable in this codebase's own deployment configs,
it just isn't the reference-prod default yet.)

---

## 4. What the OS/container boundary must actually carry

Given §3, here is what a real hard boundary looks like, concretely:

- **A dedicated non-root system user** for the agent process, with systemd
  hardening directives (`NoNewPrivileges=true`, `ProtectSystem=strict`,
  `ProtectHome=true`, `PrivateTmp=true`, `ReadWritePaths=` scoped to exactly
  `POLYROB_DATA_DIR`). This is the single highest-leverage change against §3(e) —
  it doesn't require touching any code, just the unit files.
- **`CODE_EXEC_BACKEND=docker`** (never `local_subprocess`) for any deployment
  that isn't a single, fully-trusted operator on their own box. `sandbox_guard.py`
  already enforces this server-side; the only way to weaken it is
  `POLYROB_LOCAL=1`, so don't set that flag on a shared or network-facing host.
  See `tools/code_exec/SANDBOX_SECURITY.md` for exactly what the container
  hardening (`--network none`, `--cap-drop ALL`, read-only rootfs, non-root user,
  scrubbed env, pid/memory/cpu caps) does and doesn't cover.
- **A curated MCP server allowlist**, treated as fully-trusted operator
  configuration — never something a tenant or a correspondent can add to, given
  §3(c)'s full-env inheritance.
- **Separate instances/containers per tenant** for genuinely adversarial
  multi-tenant trust. POLYROB's multi-tenant model is **single-process,
  multi-session**: tenancy is enforced by `user_id`-scoped software checks
  throughout (memory recall, goal board, correspondent registry, MCP rate
  limiting) — real, tested, and load-bearing, but it is *also* a heuristic
  running in one shared process, exactly like §2. If your threat model includes
  one tenant attacking another (not just a tenant attacking themselves), the OS
  boundary that actually matters is *between tenants' processes*, which today
  means separate deployments (`POLYROB_INSTANCE_ID` + separate
  `POLYROB_DATA_DIR`), not a flag inside one shared server.

---

## 5. Recommendations by deployment shape

**Local CLI** (`polyrob` / `polyrob run`, `POLYROB_LOCAL=1`, your own machine) —
you *are* the trust boundary. `local_subprocess` code exec and host LSP/git
subprocesses run as you, with your privileges, same as running `pytest` or
`git commit` yourself would. This is fine on a machine only you use; it is not
fine on a shared machine, and `POLYROB_LOCAL` should never be set on one.

**Single-owner VPS** (the Rob #1 shape — headless agent, one owner,
`POLYROB_LOCAL=1` on a private box you control) — the code-level trust decisions
are the same as local CLI, but the box itself is network-facing. Prioritize:
harden the systemd units to a non-root user (§4); if `CODE_EXEC_ENABLED` is ever
turned on, prefer `CODE_EXEC_BACKEND=docker` unless you specifically want the
local-subprocess convenience for your *own* trusted use; keep `CODING_LSP_ENABLED`
/ `CODING_SNAPSHOT_ENABLED` off unless you're actively using them, since they have
no sandbox at all (§3b); review `config/mcp_config.json` and remove any server you
wouldn't hand your full environment to.

**Multi-tenant server** — never set `POLYROB_LOCAL=1` (it collapses several
independent trust decisions to "operator == owner"); `sandbox_guard.py` already
refuses non-sandboxed code-exec here, keep it that way; leave
`CODING_LSP_ENABLED`/`CODING_SNAPSHOT_ENABLED` off (§3b has no sandbox regardless
of tenant, and these tools have no per-tenant isolation story yet); never let a
tenant add or edit `config/mcp_config.json`; if the box hosts genuinely mutually
distrusting tenants, treat "one shared server process" as itself a software
boundary and prefer per-tenant instances/containers over relying solely on the
`user_id` scoping (§4).

---

## See also

- [`SECURITY.md`](../../SECURITY.md) — vulnerability reporting, the unaudited-crypto disclaimer
- [`tools/code_exec/SANDBOX_SECURITY.md`](../../tools/code_exec/SANDBOX_SECURITY.md) — the Docker code-exec backend, hardening flag by flag
- [deployment-postures.md](deployment-postures.md) — the web console's `local`/`own_ops`/`multitenant` posture ladder
- [configuration.md](configuration.md) · [`../CONFIGURATION.md`](../CONFIGURATION.md) — every flag named on this page, with its default and code anchor
- [payments.md](payments.md) — the money-specific safety model (§1 there) in full
