# Security & trust model

This page answers "what stops the agent from doing something bad?" in one place.
Short version: almost every gate described elsewhere — correspondent taint, the
tool-capability table, approval hooks, money caps — is a **policy check running inside
the same process as the agent itself**. None of them is a hard security boundary. The
only hard boundary is the **operating system / container**, and this page names where
that boundary exists, where it does not, and what to do about it for each way you run
polyrob.

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
| **Fetched web content / browser page content / MCP tool outputs** | Untrusted DATA | Anything from `browser`, `web_fetch`, `http_request`, `mcp`, `perplexity`, `anysite`, `email` bodies, etc. may contain adversarial text (indirect prompt injection) |
| **Skill content** authored by the agent itself (writable skills) | Untrusted-by-default | Content is threat-scanned before write; a forged/background turn can never auto-activate or patch an active skill |
| A **group member** in an allowlisted room | Untrusted, room-scoped | May trigger a turn, but that turn runs a public session on a read-only toolset — see §2's room rail |
| An **unknown or unverified sender** on any surface | Denied | No routing authority — see the four tiers below |

The dispatcher (`core/surfaces/dispatcher.py::route_inbound`) resolves exactly one
tier per inbound message, fail-closed once the access model is on
(`CORRESPONDENT_ACCESS_ENABLED`): **OWNER**, **CORRESPONDENT**, **GROUP_MEMBER**,
or **DENIED** (`core/surfaces/access.py::AccessTier`). A correspondent's reply can
*never* reach COMMAND/STEER/TASK_AGENT — it is delivered as a
`MessageOrigin.CORRESPONDENT` control message inside a `<correspondent-message>`
envelope, with the text itself in an inner `<untrusted_tool_result>` wrapper, exactly
like a fetched web page. A group member
can only ever reach the room rail.

The tier is only as good as the proof of who sent the message, so each surface
proves it before the dispatcher sees the message. On every network surface except
Telegram (which has its own allowlist, checked first), a DM from anyone who is
not the owner or a paired sender is DENIED, even when the correspondent model is
off. An email sender reaches OWNER or CORRESPONDENT only when the receiving
server's topmost `Authentication-Results` shows `dmarc=pass` or an aligned DKIM/SPF
pass; a forged `From` gets no tier. A WhatsApp webhook without
`WHATSAPP_WEBHOOK_SECRET` rejects every payload, and a Signal surface refuses a
plain-HTTP `signal-cli` daemon on a non-loopback host.

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
the wrong one is silently dead. The gate keys on action names; that is a property of
the code, not of the mechanism.

It also does not read the text. The taint is a fact about where the turn's context
came from, not a judgement of whether the content was hostile.

### Standing work and the money regime

Every goal and cron job carries the same standing authority: when it runs, its rig
and tool list are used as written, it may post on X without a per-run approval,
and it may moderate a room only when its task text names that room. What a turn may
WRITE is decided when the work is created: a genuine owner turn may ask for any rig;
an autonomous goal or cron run may ask only for tools inside the self-goal ceiling,
and only a genuine owner turn may put a write verb on a job
(`tools/goal_tools.py::owner_seat_turn`).

Money from unattended work follows the money regime
(`core/config_policy/money_regime.py`), one of three. Under `supervised`
(`AUTONOMY_MODE` is not effectively `autonomous`) no goal or cron run moves funds
unattended: the guard's autonomous lane and the x402 autonomous lane refuse. Under
`autonomous` the agent runs its own work, but money is not armed. `armed` needs
`DEFI_AGENT_AUTONOMY`, `DEFI_TRADE_ENABLED`, `DEFI_AUTONOMOUS_TURN_TRADING`,
`DEFI_TIERED_SPEND_LANE` and a daily cap; then a run trades within the caps and the
per-verb simulation. The posture card names the regime and any missing key.

**A late approval sends exactly what was approved.** When the owner approves a
queued outbound write (an X post, reply or DM, a mail) after its run has ended, the
agent process sends those approved parameters once. It never re-runs the job or
goal that asked, so an approval of one post can never repeat the rest of that run.

### Tool-capability table (`core/tool_capabilities.py`)

The single SSOT for `money` / `high_impact` / `delegate_blocked` / `exec` per
tool_id — derives `MONEY_TOOLS`,
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
set (`core/verb_policy.py`) the Controller unions in automatically at
`AGENT_COMPUTE_POSTURE >= 2`. The durable `owner_queue` provider makes this
remotely-approvable and restart-surviving. Under `AUTONOMY_MODE=autonomous` most
approvals become allow-audit-notify; the verbs marked `always_queued` there
(self-modification, a dangerous shell command) stay on the owner
queue.

What it stops: a gated action executing without an explicit approval decision,
even across a process restart.

What it does not do: validate that the *approved* action is itself safe — approval
is a human/policy checkpoint, not a sandbox. And it only covers tools someone
remembered to put in `APPROVAL_REQUIRED_TOOLS` (or the posture-derived defaults).

### Money gates (`modules/x402`, `core/wallet/`)

Every money verb passes a stack of checks in the agent process, each fail-closed on a
probe error:

- **The owner pause** (`polyrob autonomy pause`, `/pause`, `/halt`) sits inside
  `PolicyGate.check` (`core/wallet/policy.py`) and is probed again by `tx_guard` and the
  x402 pay path. A genuine owner turn passes it
  (`core/money/authority.py::owner_direct_turn`); a call with no execution context is
  never the owner. The venue trade gate (markets pack) refuses every order while paused.
- **Turn origin.** A forged self-wake, delegation-result, leaf or autonomous turn cannot
  reach a money verb — enforced independently in `core/wallet/tx_guard.py`,
  `tools/x402/spend_gate.py`, the markets pack's `trade_turn_refusal` and the
  owner-queue approver. `DEFI_AUTONOMOUS_TURN_TRADING` and `DEFI_MONITOR_EXITS` are the
  two flag-gated narrowings; both still ride every cap.
- **Caps as a ladder** — per-transaction, rolling daily, per-venue, and the
  `DEFI_AUTONOMOUS_MAX_USD` autonomous ceiling, with a replay guard in the same gate.
- **Approval** — `PAYMENT_APPROVAL_MODE` and the receive-side invoice caps.
- **Correspondent taint** denies the money verbs by action name (`defi_trade_*`,
  `x402_pay_x402_fetch`, `x402_invoice_x402_request`, the venue order verbs).
- **The RPC pin** — `tx_guard` refuses to move funds on the shared public RPC.
- **`secret_guard`** (`core/security/secret_guard.py`) denies every agent-writable file
  surface access to credential-shaped files, including the wallet's `meta.json` and
  `audit.jsonl`.
- **A payout shape always waits for the owner**
  (`core/config_policy/spend_lane.py::payout_shaped`), in every lane.
- **No default network** — every money verb and wallet command needs an explicit chain.
- **Tamper evidence** — the spend ledger is chain-hashed and sealed with a key from the
  master seed, and every on-chain submission is journaled
  (`wallet/submissions.sqlite`) before it is signed; an unresolved broadcast blocks new
  submissions.

The detail of each check is in [wallet.md](wallet.md) (caps, audit, address poisoning,
transfers to yourself, x402) and [onchain.md](onchain.md) (the transaction guard and its
ordered gates); the arming order is in
[payments.md](payments.md#9-arming-it-one-step-at-a-time).

What it does not do: protect the wallet's *key material* from a host-level
compromise — if the process itself is compromised (see §3's "process identity" gap),
the limits are enforced by the same process an attacker would already control. The
wallet signer below is the lever that moves the key out of that process.

### The wallet signer (`core/signer/`)

`WALLET_SIGNER` (default `local`) decides where signatures come from. Under `local`
the agent's own process holds the seed and signs, and every gate above runs in that
process. Under `shadow` the agent still signs and also logs the signer's verdict. Under
`remote` the agent holds no key: `polyrob-signer` runs as its own OS user behind a Unix
socket that checks the connecting UID, re-runs `tx_guard` on its own pinned RPC against
the hard limits in `signer.toml`, keeps its own spend record, and only then signs; above
its hard limit the owner approves on the box, as root. This is the one money gate that
does not share the agent's process. Set-up and cut-over:
[wallet.md](wallet.md#where-the-signatures-come-from--the-signer).

### The on-chain transaction guard (`core/wallet/tx_guard.py`)

No value-moving EVM transaction is broadcast without simulating it, measuring the
observed asset and allowance deltas, and asserting them against the declared intent;
`solana_swap` mirrors the same step order against `simulateTransaction`. The gates are
listed in [onchain.md](onchain.md#defi_trade--value-movement-behind-a-transaction-guard).

There are two money-gate architectures, split by mechanism: **simulate-and-assert**
(`tx_guard`) governs everything that signs a raw transaction; the **declared-amount**
model (the PolicyGate and the markets pack's venue `trade_gate`) governs venue orders
and x402 payments, where there is no local transaction to simulate. The money-verb
ratchet (`tests/unit/core/test_money_verb_registration.py`) requires every new money
verb to name its gate, so a new verb cannot pick the weaker gate by accident.

### Host-capability gates (`tools/shell/tool.py`, `tools/self_env/tool.py`)

Three tools reach the host on purpose, and each is gated by the compute posture
rather than by a plain on/off flag:

- **`shell`** (`shell_run`) and **`process`** (`process_list/poll/wait/log/kill`,
  plus `process_write/submit/close` for a background job's stdin) —
  the persistent dev shell and its job manager. A background job can run under a
  PTY on the host and notify the session on exit or on an output pattern; its
  redacted receipt is kept for 7 days. `process_write` refuses to type into what
  looks like a password prompt. Every action asks
  `compute_posture_allows(execution_context, 1)`: posture ≥ 1, owner tenant, not
  a leaf or sub-agent, not a forged self-wake or delegation-result turn.
  `SHELL_TOOLS_ENABLED` defaults ON at posture ≥ 1 and OFF below it. Commands run
  in the session's container, or on the host at posture 3 (§3d).
- **`self_env`** (`read_source`, `restart_service`) — the agent reading its own
  source and asking for its own restart, as distinct verbs, never raw bash
  (updates are the owner's `/update`). Every verb asks `compute_posture_allows(ctx, 2)` **and**
  is approval-gated: at posture ≥ 2 the Controller unions `shell_run` and every
  `self_env_*` verb into the gated set and defaults the approval provider to
  interactive. Each call emits a `self_modification` audit event.
  `SELF_ENV_ENABLED` defaults ON at posture ≥ 2.

**The command guard** (`core/security/command_guard.py`) classes every shell
command before it runs. The **floor** — deleting a root or home tree, a fork bomb,
writing a raw disk, a root `curl | sh`, writing the agent's own config, wallet or
signer state in one command segment, reading the wallet seed, stopping the agent's
own service — is refused at every posture, inside the container too, and no approval
lifts it. The floor is judged per command segment: `grep -r ERROR
/var/lib/polyrob/logs > errors.txt` reads a protected path and writes an ordinary
file, so it is an owner decision, not the floor. The **dangerous** class
(recursive delete, force-push, `sudo`, container and service control, nested
interpreters, environment dumps, pipe-to-shell) waits for the owner wherever
`shell_run` is approval-gated. In an unattended run nobody can answer, so the
owner's `SECURITY_SETTINGS` row `unattended_dangerous_shell` decides: `sandbox`
(default) runs it inside the sandbox, never on the host; `refuse` refuses it. With `SHELL_APPROVAL_MODE=per_command` (the default) a safe
command skips the wait; `every` makes every gated call wait; `smart` lets the aux model
deny a dangerous command or approve it — but only inside a sandbox, never on the host.
`SHELL_DENY` / `SHELL_ALLOW` (and the `shell.deny` / `shell.allow` prefs) refuse or pre-approve
command patterns; an allow entry never lifts the floor. The approval prompt is keyed by the
command, so "always" or "never" applies to that command line, not to every shell call.

The guard reads the whole line, not only its first word. A `$(…)` or backtick
substitution is classified with the same guard; a wrapper, a brace group or a
blank-line statement is judged for every command it may run; text the parser cannot
read is dangerous. A relative recursive delete below the working directory runs
without the owner, but the shell tool checks it again after resolving symlinks, and
refuses it when the same line creates a symlink or when it reaches the data home.
A `sqlite3` write to one of the agent's own state databases (cron, goals, memory,
cards, the wallet's submission journal and the rest) needs the owner whatever the path looks like. The agent's own
workspace is not protected state, but a `.polyrob` state dir or an env file inside
it still is.

All three are `exec` + `high_impact` + `delegate_blocked` in the capability
table: never in a default toolset, never given to a delegated sub-agent, and
refused on a correspondent-tainted turn.

What it stops: an autonomous, delegated, forged or non-owner turn from reaching
the host at all, and — at posture ≥ 2 — self-modification without an explicit
decision.

What it does not do: sandbox the command once it is approved. `shell_run` runs
inside the session's dev container (or on the host at posture 3); `self_env` runs
against the install tree itself. The guard is pattern-based: it stops the known
destructive shapes, not a determined author of new ones. The posture is the choice of how much host you are handing over — leave
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

### The agent's mailbox (the `email` tool)

The agent can own a full mailbox: its own AgentMail address (`AGENTMAIL_API_KEY`)
or an IMAP/SMTP account (`EMAIL_PROVIDER=smtp`). It has ten verbs:

- **Read:** `email_list` (newest first, with filters and a Gmail `query`),
  `email_read` (headers, text body, links, the attachment list; this includes
  activation links and codes, the mail the inbound surface never answers) and
  `email_save_attachment` (into the session workspace).
- **Send:** `email_send` (to, cc, bcc, HTML, workspace attachments), `email_reply`
  (threaded, the original quoted) and `email_forward` (with the original attachments).
- **Organise, IMAP only:** `email_folders`, `email_mark`, `email_move` and
  `email_delete` (to Trash; there is no expunge). These need an owner turn.

Every send verb goes through one gate: forged and autonomous turns, the tier of
EVERY recipient (one denied address refuses the whole send and names the owner's
`/allow email` command), the owner pause, the secret scrub and the resend cooldown.
The read verbs open their own read-only IMAP connection and never mark a mail as
seen. Mail is third-party text: every result is framed as untrusted.

### The pattern across all of them

Every one of these is **software logic evaluated by the same interpreter that
runs the agent loop**. Each checks a fixed condition before a tool runs; none of them
separates memory, files or privileges. A bug in one of them, or a path none of them
covers, is not stopped at this layer. That is why they need a second, independent line
of defense underneath them: the OS/container boundary.

---

## 3. The gaps — what runs unsandboxed on the host today

These are documented, deliberate trade-offs, not bugs. Each one names what runs
outside a sandbox and what to do about it.

### a) Host work runs beside the wallet; the key is isolated in memory, not on disk

Wallet custody does not refuse host work. Git, LSP diagnostics, coding snapshots,
MCP stdio, `rg`, lazy installs and `self_env` run with the wallet present, in every
signer setting. The process memory is protected by isolation: the seed leaves
`os.environ` when the wallet settings load, the process is non-dumpable (a same-UID
child cannot read `/proc/<pid>/environ`), and every host child gets an allowlisted
env with no custody secret. Two things refuse every host child
(`core/security/host_execution.py::host_execution_refusal`): the floor row
`key_isolation_gap` (a key is held and the non-dumpable hardening did not hold), and
the owner's `SECURITY_SETTINGS` row `host_exec_under_custody=refuse`.

That isolation does not cover a file. A host child runs as the agent's own OS user,
so it can read every file that user can read. A local `polyrob wallet init` writes
the seed to `~/.polyrob/.env` (0600, the same user), so a local wallet's key is
**not** isolated from host children unless the signer holds it. For that case
(`core/security/host_execution.py::model_code_host_refusal`): when an env file
the agent's user can read holds a wallet key, the children that run code the model wrote
refuse on the host — `run_tests`, `run_code` and the posture-3 host shell. The
files checked are every env file this process loaded at start and every env file
it wrote a wallet key into (`polyrob wallet init`, `polyrob config set`). Each check
reads the file again, so the file decides, not what the process holds: a seed left
in the file under the remote signer, or an older seed than the one the service
manager set, refuses too, and the refusal ends when the seed leaves the file. The
refusal names the fixes: move the seed to the signer (`WALLET_SIGNER=remote`, see
the wallet signer in §2) and remove it from that file, or run code in a sandbox backend
(`CODE_EXEC_BACKEND=docker`). The other host children above stay allowed: they do
not run code the model wrote. A seed only in the service manager's env file (the
server shape: `wallet.env`, root-owned 0600) and an install with no wallet see
no change. The env copies in an old `polyrob update` snapshot
(`<data home>/snapshots/*/config/`) are checked too: a seed moved to the signer can
still sit there. That refusal names its own fix — delete the old snapshot once you
no longer need to roll back to it, or use a sandbox backend; the agent never edits a
snapshot, so a restore keeps the seed. A backup you made elsewhere is not checked.

Servers always require a sandbox backend for `run_code`. Under a remote signer a
host child can call the signer socket; the signer's limits and on-box approvals bound
it.

### b) Host Git, diagnostics and snapshots

`tools/git/tool.py`, `tools/coding/lsp.py` and `tools/coding/snapshot.py` ask the
same rule before spawning host binaries. They run with the host user's privileges
and must only consume trusted workspaces and configuration.

### c) MCP stdio children

MCP children are spawned through `tools/mcp/child_env.py::build_mcp_child_env`
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

**Launch restrictions.** Chromium sandboxing is explicitly enabled for local launches. Disabling it is
limited to explicit local development without custody. A process holding an agent
or payment master seed, or enabling the agent wallet, cannot launch local Chromium
or attach through the existing-local-Chrome helper. It must use a separately
isolated remote browser. Remote endpoint isolation and connection-time egress
restrictions are operational requirements, not properties verified by the URL.
The Playwright driver is still trusted host code; independent signer isolation
remains necessary.

### d) `AGENT_COMPUTE_POSTURE=3` runs the shell on the host

At posture 3 the `shell` and `process` tools run commands directly on the machine,
as the agent's own user, with the real filesystem
(`tools/shell/host_executor.py`). `core/security/host_execution.py::host_shell_refusal`
admits a turn only when ALL hold:

1. `compute_posture_allows(ctx, 3)` — the owner's own, non-delegated, non-forged turn;
2. `POLYROB_LOCAL` is on;
3. `model_code_host_refusal` passes (key isolation holds, no env file the agent's
   user can read holds a wallet key — see §3a — and the owner's
   `host_exec_under_custody` row allows it);
4. the turn was typed at a foreground terminal of this process (`polyrob` or
   `polyrob run`). Telegram, the API, `/v1`, A2A, rooms and the web console never
   qualify — not even the console on loopback, because behind a reverse proxy every
   request arrives from 127.0.0.1.

Every other turn keeps the container, and a failed container is a refusal, never
the host. The child environment is the same allowlist scrub as `run_code` (no
`*_API_KEY`, `*_TOKEN` or seed reaches it), `sudo` is refused on the host, the
output passes the credential-shape redaction before it enters the transcript, and
each command leaves one `host_exec` event (a hash of the command, the exit code,
the time — never the text or the output). A server deploy should refuse an env file
that sets posture 3 or `SHELL_BACKEND=host`.

What it does not do: confine the command. Once a safe-class or approved command
runs on the host, it can do anything your user can do.

`sudo` on the host stays refused unless `SHELL_HOST_SUDO=prompt`: then you type the password
at your terminal for that one command, and it reaches `sudo` on stdin only — never cached,
logged or put in the environment. `SHELL_HOST_INIT_FILES` sources your shell files into each
command; anything they export becomes visible to the agent's commands.

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
  agent interpreter is not isolated custody, even after a non-root migration: the
  signer runs from a venv of its own, and with `[server] peer_check = main_process`
  in `signer.toml` it serves only the agent unit's main process, never a shell or
  MCP child of the same UID (`warn`, the default, logs such a child and still
  serves it). The agent's `HOME` belongs outside the shared data home, so another
  identity that can write that tree cannot plant state the agent reads.
- **`CODE_EXEC_BACKEND=docker`** (never `local_subprocess`) for any deployment
  that isn't a single, fully-trusted operator on their own box. `sandbox_guard.py`
  enforces this server-side and in custody processes, including local mode.
  See `tools/code_exec/SANDBOX_SECURITY.md` for exactly what the container
  hardening (`--network none`, `--cap-drop ALL`, read-only rootfs, non-root user,
  scrubbed env, pid/memory/cpu caps) does and doesn't cover. Related levers:
  `CODE_EXEC_NETWORK=proxy` puts the persistent sandbox on an internal network
  behind one allowlist proxy (package registries by default, `CODE_EXEC_EGRESS_ALLOW`);
  the persistent dev sandbox otherwise has open egress. `CODE_EXEC_DOCKER_BINARY=podman`
  swaps the container CLI. The `ssh` backend pins the remote host key with
  `CODE_EXEC_SSH_KNOWN_HOSTS` (a missing file is refused; unset means trust on first
  use). The `modal`, `daytona`, `vercel_sandbox` and `singularity` packs add remote
  or rootless backends (`SHELL_BACKEND=<name>`); they are tested against fakes only.
  `run_code(tools=True)` (`CODE_EXEC_TOOL_CALLS`, default OFF) lets a script call an
  allowlist of agent tools over a per-run socket, and every call re-enters the
  Controller, so every gate and approval above still applies.
- **A curated MCP server allowlist**, treated as fully-trusted operator
  configuration — never something a tenant or a correspondent can add to: the
  child env is allowlisted now (§3c), but the server binary itself still runs
  with the agent's full host privileges.
- **Separate instances/containers per tenant** for genuinely adversarial
  multi-tenant trust. POLYROB's multi-tenant model is **single-process,
  multi-session**: tenancy is enforced by `user_id`-scoped software checks
  throughout (memory recall, goal board, correspondent registry, MCP rate
  limiting), but it is *also* a policy check
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

**Single-owner VPS** (headless agent, one owner,
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

## 6. What the agent records, and the daily scan

The gates above decide. The security ledger makes each decision visible. Every
lane writes a typed row to `telemetry_events.db` (no message text — a length and
an 8-character hash at most), and `core/security_digest.py` is the ONE rollup that
the `security` status section and the daily brief's `SEC` line both read:

- **Perimeter** — every inbound routing decision, allowed (`inbound_routed`) and
  refused (`access_denied`, with a reason slug).
- **Refusals** — gate refusals (`tool_denied`, `source=gate`): correspondent
  taint, approvals, forged turns, money gates, the room toolset.
- **Threat flags** — content the threat scanner flagged (`injection_flagged`).
- **Limiter trips** — a named perimeter limiter (the API middleware, the public
  x402 invoice endpoints, the console's connection and event throttles) records
  one `rate_limited` row per limiter and key per window, so a flood writes one
  row, not thousands. Pacing limiters (outbound delivery, the X post budget) stay
  silent.
- **Cost per chat** — model spend from `usage_records`, joined to the chat that
  owns the session through `session_chat_map`. Spend is per chat: a group room
  is one shared session, and no store records one member's share of it.

A deep security scan is operator-supplied: the package does not ship one. The repository
ships `requirements.lock` (for `pip-audit`) and `.gitleaks.toml` (for `gitleaks`), and a
useful scan also checks the host (modes under `/etc/polyrob/`, non-root units,
`WEBVIEW_READ_ONLY` and `POLYROB_POSTURE` set, TLS certificate expiry). A scan that
writes its verdict to `<data>/ops/security_scan.json` shows on the `SEC` line as
`scan clean`, `scan findings N`, `scan incomplete (<checks> not run)`, `scan stale (...)`
or `scan not run`. A check that could not run is named as not run and is never a pass.

---

## See also

- [`SECURITY.md`](../../SECURITY.md) — vulnerability reporting, the unaudited-crypto disclaimer
- [`tools/code_exec/SANDBOX_SECURITY.md`](../../tools/code_exec/SANDBOX_SECURITY.md) — the Docker code-exec backend, hardening flag by flag
- [deployment-postures.md](deployment-postures.md) — the web console's `local`/`own_ops`/`multitenant` posture ladder
- [configuration.md](configuration.md) · [`../CONFIGURATION.md`](../CONFIGURATION.md) — every flag named on this page, with its default and code anchor
- [payments.md](payments.md#1-the-safety-model-read-first) — the money safety model in full; [wallet.md](wallet.md) and [onchain.md](onchain.md) for the wallet and on-chain gates
