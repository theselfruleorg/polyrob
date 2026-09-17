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
| A **group member** in an allowlisted room | Untrusted, room-scoped | May trigger a turn, but that turn runs a public session on a read-only toolset — see §2's room rail |
| An **unknown or unverified sender** on any surface | Denied | No routing authority — see the four tiers below |

The dispatcher (`core/surfaces/dispatcher.py::route_inbound`) resolves exactly one
tier per inbound message, fail-closed once the access model is on
(`CORRESPONDENT_ACCESS_ENABLED`): **OWNER**, **CORRESPONDENT**, **GROUP_MEMBER**,
or **DENIED** (`core/surfaces/access.py::AccessTier`). A correspondent's reply can
*never* reach COMMAND/STEER/TASK_AGENT — it is delivered as a
`MessageOrigin.CORRESPONDENT` control message, framed with an
`<untrusted_tool_result>` wrapper, exactly like a fetched web page. A group member
can only ever reach the room rail.

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

More is enforced here than caps. The full set, composed (every one fail-closed
on a probe error):

- **The owner pause** (`polyrob autonomy pause` / `/pause` / a plain "stop" from the
  owner; the legacy `AUTONOMY_HALT` file/env is a facet of it) refuses
  ALL money movement — it is structurally inside `PolicyGate.check`
  (`core/wallet/policy.py`), so every money verb inherits it, and it is probed
  again at the top of `tx_guard.authorize`, the x402 pay path, and the venue
  trade gate.
- **Turn-origin refusal**: a forged self-wake, delegation-result, leaf, or
  autonomous turn cannot reach a money verb — enforced independently at
  `core/wallet/tx_guard.py` step 2 (on-chain), `tools/x402/spend_gate.py`
  (x402 pay), `tools/crypto_trade_gate.py::trade_turn_refusal` (venue orders),
  and the owner-queue approver. Two deliberate, flag-gated narrowings exist:
  `DEFI_AUTONOMOUS_TURN_TRADING` admits goal/cron-dispatched runs, and
  `DEFI_MONITOR_EXITS` admits EXIT-shaped operations only (close a held
  position into the quote asset, measured inflow asserted) from a forged
  main-agent turn. Both default OFF; both still ride every cap.
- **Caps as a ladder**: `AGENT_WALLET_MAX_PER_TX_USD` (catastrophic ceiling),
  `WALLET_DAILY_CAP_USD` (rolling 24h), per-venue caps, the
  `DEFI_AUTONOMOUS_MAX_USD` autonomous ceiling (above it → the durable
  `owner_queue` lane, never auto-execute), and a daily-cap-REQUIRED bar for
  any unattended origin. An idempotency replay guard sits in the same gate.
- **`PAYMENT_APPROVAL_MODE`** (`approve` default routes every outward payment
  through `owner_queue`; `auto` auto-approves within caps and notifies after),
  plus `X402_INVOICE_MAX_USD` / `X402_INVOICE_DAILY_MAX` on the receive side.
- **The correspondent-taint gate** denies the money verbs by action name
  (`defi_trade_*`, `x402_pay_x402_fetch`, `x402_invoice_x402_request`, the
  venue order verbs) while a session is tainted — not only by tool_id.
- **The RPC pin**: `tx_guard` refuses to move funds on the shared public RPC
  (`DEFI_EVM_RPC_<CHAIN>`, `DEFI_SOLANA_RPC`) — the simulation, the deltas and
  the caps are all read from it, so it cannot be the trust anchor by default.
- **`secret_guard`** (`core/security/secret_guard.py`) hard-denies every
  agent-writable file surface access to credential-shaped files — `.env*`,
  `*.env`, key material, and the wallet's own `meta.json`/`audit.jsonl` (whose
  rewrite would reset the rolling caps or flip an address).

Persistent policy history refuses damaged storage and invalid amounts. A shared
POSIX lock serializes check/spend/record across cooperating local processes.
This does not make settlement atomic with recording: a crash after broadcast can
leave a completed payment unrecorded. Durable pre-broadcast reservations and
reconciliation are still required to close that window.

What it does not do: protect the wallet's *key material* from a host-level
compromise — if the process itself is compromised (see §3's "process identity" gap),
the caps are enforced by the same process an attacker would already control.

### The on-chain transaction guard (`core/wallet/tx_guard.py`)

Distinct enough from the caps to name on its own: **no value-moving EVM
transaction is broadcast without simulating it, measuring the observed asset
and allowance deltas, and asserting them against the declared intent** — a
revert, an RPC error, an unreadable balance, an undeclared allowance grant, an
unpriceable outflow, or a measured-zero outflow on a declared send all REFUSE.
Effects are measured, never inferred from the caller's calldata. The
`solana_swap` verb cannot route through `tx_guard` (there is no EVM
transaction), so it mirrors the same step order against `simulateTransaction`
deltas, with the SPL authority taxonomy (delegate/owner/close/freeze grants
refuse) standing in for the allowance check.

There are two money-gate architectures, split by mechanism, and the split is
deliberate: `tx_guard`-style **simulate-and-assert** governs everything that
signs a raw transaction; the PolicyGate/`crypto_trade_gate` **declared-amount**
model governs venue orders and x402 payments, where there is no local
transaction to simulate. The risk is a NEW money verb picking the weaker gate
out of convenience — the bidirectional money-verb ratchet
(`tests/unit/core/test_money_verb_registration.py`) is the enforcement point:
any new money verb must name its gate.

### Host-capability gates (`tools/shell/tool.py`, `tools/self_env/tool.py`)

Three tools reach the host on purpose, and each is gated by the compute posture
rather than by a plain on/off flag:

- **`shell`** (`shell_run`) and **`process`** (`process_list/poll/log/kill`) —
  the persistent dev shell and its job manager. Every action asks
  `compute_posture_allows(execution_context, 1)`: posture ≥ 1, owner tenant, not
  a leaf or sub-agent, not a forged self-wake or delegation-result turn.
  `SHELL_TOOLS_ENABLED` defaults ON at posture ≥ 1 and OFF below it.
- **`self_env`** (`install_dep`, `read_source`, `patch_source`, `git_pull`,
  `restart_service`) — the agent patching and restarting itself, as distinct
  verbs, never raw bash. Every verb asks `compute_posture_allows(ctx, 2)` **and**
  is approval-gated: at posture ≥ 2 the Controller unions `shell_run` and every
  `self_env_*` verb into the gated set and defaults the approval provider to
  interactive. Each call emits a `self_modification` audit event.
  `SELF_ENV_ENABLED` defaults ON at posture ≥ 2.

All three are `exec` + `high_impact` + `delegate_blocked` in the capability
table: never in a default toolset, never given to a delegated sub-agent, and
refused on a correspondent-tainted turn.

What it stops: an autonomous, delegated, forged or non-owner turn from reaching
the host at all, and — at posture ≥ 2 — self-modification without an explicit
decision.

What it does not do: sandbox the command once it is approved. `shell_run` runs
inside the session's dev container; `self_env` runs against the install tree
itself. The posture is the choice of how much host you are handing over — leave
`AGENT_COMPUTE_POSTURE` at `0` unless you are deliberately building software
with the agent, and `3` requires `POLYROB_LOCAL` on a single-tenant box (§3d).

### The room rail (`core/surfaces/room_policy.py`)

A session bound to a group chat is **PUBLIC**, and that is a capability decision,
not a formatting one. A public session carries none of the owner tenant's private
state (no owner facts, no SOUL/SELF docs, no tenant memory recall or write, no
episodic digest, no project context — each injector asks `is_public_session()`
and returns early), and it runs a fixed read-only toolset (`GROUP_TURN_TOOLS`,
default `task,web_fetch,defi_data`) that the environment can only narrow, never
widen. A fail-closed pre-tool hook denies every room-denied call by name:
money by capability bit, the whole `kb_*` family, deferred execution
(`goal_create`, `cronjob_schedule`, `skill_manage`, `load_tool`, …), cross-session
recall, outbound to anywhere but this room, and delegation or control verbs.
Every room turn is stamped `turn_kind="group"`, which the same forged-turn
predicate that protects self-wakes treats as forged — so `tx_guard` refuses to
sign for it even if a schema slipped through.

What it stops: a stranger in a room from reading the owner's state, spending,
scheduling work a later owner-tenant run would execute, or making the agent speak
anywhere else.

What it does not do: change the fact that this is the same in-process pattern as
everything else in §2 — a room is a second line behind the allowlist, not a
sandbox. Full behaviour, roles and caps: [groups.md](groups.md#what-the-agent-can-and-cannot-do-in-a-room).

### The pattern across all of them

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

### a) Local host execution is unavailable in custody processes

`tools/code_exec/sandbox_guard.py` requires a sandbox-capable backend whenever
`AGENT_WALLET_ENABLED` is true **or** a supported master seed is present
(`AGENT_WALLET_MASTER_SEED`, `PAYMENT_MASTER_SEED`, or legacy `MASTER_SEED`).
`POLYROB_LOCAL=1` cannot bypass this requirement. Local instances without custody
retain the host subprocess option; servers always require a sandbox backend.
This gate controls the supported tool path, not arbitrary trusted Python code.

### b) Host Git, diagnostics and snapshots refuse alongside custody

`tools/git/tool.py`, `tools/coding/lsp.py` and `tools/coding/snapshot.py` check the
same custody predicate before spawning host binaries. Git returns a refusal;
diagnostics and snapshots skip their optional work. They do not route into
Docker. In a non-custody process these helpers still run with the host user's
privileges and must only consume trusted workspaces and configuration.

### c) MCP stdio is refused in custody processes

MCP stdio refuses to start when the wallet is enabled or a seed is present.
Use a separately isolated HTTP service instead. Without custody, MCP children
are spawned through `tools/mcp/child_env.py::build_mcp_child_env`
(called from `tools/mcp/protocol.py`), which constructs the child env from a
**fixed allowlist** (PATH/HOME/locale/runtime-lookup vars only).
Credential-shaped variables — `AGENT_WALLET_MASTER_SEED`, LLM provider keys, DB
credentials — are excluded by construction, not by name-matching, and this is
the only stdio spawn site in the package.

What remains true, and is the residual trade-off to respect: an MCP server is
still a **full sibling host process** with the agent's own OS privileges (no
sandbox), and values the operator explicitly writes into `config/mcp_config.json`
are overlaid verbatim and deliberately NOT secret-filtered — that file is the
sanctioned credential channel for a server that needs one, so anything you put
there is handed over. Treat the server *binary* as fully-trusted code even
though its environment is now scrubbed.

### c2) The browser runs as its own principal — a boundary only when the sandbox and egress are on

A custody process never launches Chromium (`tools/browser/launch_security.py`);
it connects to `polyrob-browser.service` over CDP (`BROWSER_CDP_URL`). That
service is a boundary exactly to the extent that three things hold, all of which
`polyrob browser install` sets up and `polyrob browser status` reports:
a dedicated UID with no custody environment; the **Chromium sandbox on** (an
AppArmor `userns` profile for the binary — never `--no-sandbox`); and a
**UID-keyed egress chain** so the browser cannot open connections to loopback
services, RFC1918 or the metadata service. What it does not remove is the shared
kernel; a second host over `BROWSER_WSS_URL` does. Every session gets a fresh
context over CDP, so no login persists in the service's profile; CDP itself is
unauthenticated on loopback, which is why nothing may persist there.

### d) `AGENT_COMPUTE_POSTURE=3` ("host") is designed but unwired

`core/config_policy/policy.py` defines the posture (0 `confined` / 1 `sandbox-dev`
/ 2 `self-maintain` / 3 `host`) and the single gate predicate
`compute_posture_allows(execution_context, min_posture)`, but **no code path in
the tree currently calls it with `min_posture=3`**. Nothing today grants
host-tier capability through this lever — it is a reserved, not an active,
capability. If a feature is ever built against posture 3, it inherits the
documented requirement: `POLYROB_LOCAL` **and** a single-tenant box, refused on
any network-facing surface.

### e) Process identity is yours to set

Nothing in POLYROB drops privilege for you. If you run `polyrob telegram`,
`polyrob dashboard` or `polyrob serve` as root — directly or from a systemd unit
with no `User=` and no `NoNewPrivileges` / `ProtectSystem` / `ProtectHome` /
`PrivateTmp` — then the process every in-process gate in §2 lives inside **has
full host root**, and those heuristic layers are the only thing between a bug in
one of them and full host compromise. Check it with `ps -o user= -p $(pgrep -f
polyrob | head -1)`; §4 says what to run it as instead.

---

## 4. What the OS/container boundary must actually carry

Given §3, here is what a real hard boundary looks like, concretely:

- **Separate non-root system users** for frontend and custody processes, with systemd
  hardening directives (`NoNewPrivileges=true`, `ProtectSystem=strict`,
  `ProtectHome=true`, `PrivateTmp=true`, `ReadWritePaths=` scoped to exactly
  `POLYROB_DATA_DIR`). This is the single highest-leverage change against §3(e) —
  shared data permissions and browser/cache paths must be checked before cutover.
  Rootful Docker group membership remains root-equivalent. A signer sharing the
  agent interpreter is not isolated custody, even after a non-root migration.
- **`CODE_EXEC_BACKEND=docker`** (never `local_subprocess`) for any deployment
  that isn't a single, fully-trusted operator on their own box. `sandbox_guard.py`
  enforces this server-side and in custody processes, including local mode.
  See `tools/code_exec/SANDBOX_SECURITY.md` for exactly what the container
  hardening (`--network none`, `--cap-drop ALL`, read-only rootfs, non-root user,
  scrubbed env, pid/memory/cpu caps) does and doesn't cover.
- **A curated MCP server allowlist**, treated as fully-trusted operator
  configuration — never something a tenant or a correspondent can add to: the
  child env is allowlisted now (§3c), but the server binary itself still runs
  with the agent's full host privileges.
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
without custody, host subprocesses run as you and require trusted inputs. With
custody enabled, code execution requires a sandbox and host Git/LSP/snapshot
helpers refuse. Local mode does not provide an OS isolation boundary.

**Single-owner VPS** (the Rob #1 shape — headless agent, one owner,
`POLYROB_LOCAL=1` on a private box you control) — the code-level trust decisions
are the same as local CLI, but the box itself is network-facing. Prioritize:
harden the systemd units to a non-root user (§4); if `CODE_EXEC_ENABLED` is ever
turned on alongside custody, use a sandbox-capable backend; keep `CODING_LSP_ENABLED`
/ `CODING_SNAPSHOT_ENABLED` off unless you're actively using them, since they have
no sandbox at all (§3b); review `config/mcp_config.json` and remove any server
whose binary you wouldn't run with the agent's own host privileges (its env is
allowlisted now, but any secret you configure for it in that file is handed over
verbatim).

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

### Browser launch restrictions

Chromium sandboxing is explicitly enabled for local launches. Disabling it is
limited to explicit local development without custody. A process holding an agent
or payment master seed, or enabling the agent wallet, cannot launch local Chromium
or attach through the existing-local-Chrome helper. It must use a separately
isolated remote browser. Remote endpoint isolation and connection-time egress
restrictions are operational requirements, not properties verified by the URL.
The Playwright driver is still trusted host code; independent signer isolation
remains necessary.
