# `code_exec` sandbox security review

Scope: `tools/code_exec/backends/docker.py::DockerBackend`, the hardened backend behind
the agent-callable `run_code` action (`tools/code_exec/tool.py`). Escape-attempt tests
live in `tests/unit/tools/code_exec/test_docker_escape.py`; pure argv-shape tests live in
`tests/unit/tools/code_exec/test_docker_backend.py`.

## Threat model

Agent-authored `code` (the string an LLM produces and asks `run_code` to execute) is
**untrusted input**, on the same footing as text scraped from a web page — it may be the
direct result of prompt injection, a model mistake, or a deliberately adversarial task.
The one invariant that must hold regardless: **that code must never touch the trusted
host process** — no host filesystem access outside an explicit scratch directory, no
host network access by default, no privilege gain, no unbounded resource consumption. The
`DockerBackend` is POLYROB's implementation of that invariant; `tools/code_exec/
sandbox_guard.py` is the enforcement layer that refuses to run *any* backend that doesn't
advertise `capabilities["sandbox"] is True` outside local/single-user mode.

## Hardening flags and what each one defends against

Every flag below is emitted unconditionally by `DockerBackend._build_run_argv` (pure,
unit-tested without Docker in `test_docker_backend.py`) and exercised live against a real
daemon in `test_docker_escape.py`.

| Flag | Defends against |
|---|---|
| `--rm` | Container is destroyed the instant the run ends — no lingering state, no artifact/history accumulation to later inspect or abuse. |
| `--network none` (default; see below) | **Data exfiltration and C2/callback access.** The container has no network stack at all — not "restricted," genuinely absent. Verified live: `socket.create_connection` raises `OSError: Network is unreachable`. |
| `--cap-drop ALL` | Removes every Linux capability (`CAP_SYS_ADMIN`, `CAP_NET_RAW`, `CAP_SYS_PTRACE`, …), closing the standard set of kernel-level privilege-escalation and container-escape primitives that a default container would otherwise retain. |
| `--security-opt no-new-privileges` | Blocks `setuid`/`setgid`/file-capability binaries from granting *more* privilege than the process already has — closes the classic "shell out to a suid binary" escalation path even if one exists in the image. |
| `--read-only` (rootfs) | Prevents persistent tampering with the container filesystem — no dropping a backdoor into `/usr/bin`, no cron persistence, no modifying the interpreter. Verified live: `touch /etc/...` fails with `Read-only file system`. |
| `--tmpfs /tmp` | The deliberate, narrow writable exception to `--read-only` — memory-backed, wiped with the container, and nowhere else on the rootfs. Most tooling (compilers, package build steps) needs *some* scratch space; this bounds it instead of loosening the rootfs generally. |
| `--pids-limit 256` (default) | Caps total processes/threads inside the container — the fork-bomb and thread-exhaustion defense. `test_pids_limit_present_in_argv` proves the flag is always present without ever running a fork bomb; `test_pids_limit_allows_bounded_process_spawn` proves the cap doesn't break ordinary bounded concurrency (20 short-lived processes). |
| `--memory 1024m` (default) | Caps container memory — the OOM/memory-exhaustion defense against the host. See *Residual risks* below for the swap nuance. |
| `--cpus 1.0` (default) | Caps CPU shares — bounds a single run's CPU burn (busy-loops, crypto-mining payloads) so it can't starve co-located work. |
| `--user <uid>:<gid>` | Non-root execution. Precedence: explicit `CODE_EXEC_DOCKER_USER` override (verbatim, even if root — an intentional operator escape hatch) → the invoking host uid:gid when non-root (keeps the bind-mounted workspace writable) → **forced `65534:65534` (nobody:nogroup) whenever the host process itself is root** (prod systemd commonly runs `User=root`; this stops that from silently becoming uid 0 *inside* the sandbox). Verified live in both branches (`test_container_does_not_run_as_root` here; `test_docker_user_forced_unprivileged_when_host_is_root` in `test_docker_backend.py` for the root-host argv shape). Non-root execution doesn't stop a kernel-level container escape, but it materially shrinks the blast radius of anything short of one (arbitrary file write/read is uid-scoped, not root-scoped). |
| `-v <workdir>:/workspace -w /workspace` | The **only** host path the container can reach at all. `workdir` is a per-request directory (the session workspace, or an ephemeral tempdir cleaned up after the run) — never the POLYROB install directory, never `$HOME`, never a shared path across tenants. Verified live: a file written to `/workspace` inside the container appears at the exact host `workdir` path afterward, and nowhere else. |
| Container env is an explicit allowlist, not inherited | The container receives **zero** host environment variables. Only the caller-supplied `ExecutionRequest.env` dict is forwarded (via `-e`), and even that is filtered through `SECRET_PAT` (`API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY|MNEMONIC|SEED|CREDENTIAL`, case-insensitive) — a caller cannot smuggle a secret-named var into the sandbox even on purpose. The *host-side* `docker run` process (the CLI invocation itself, not the container) gets the same treatment one layer up, via the shared `build_child_env` used by every backend (`tools/code_exec/env_policy.py`) — it inherits only `PATH`/`HOME`/`LANG`/etc., never the real POLYROB process environment (API keys, DB creds, wallet seeds). |
| Output cap (`CODE_EXEC_MAX_OUTPUT_BYTES`, default 100 000) + hard wall-clock timeout (`CODE_EXEC_MAX_TIMEOUT_SEC`, default 30s, `SIGKILL`'d by process group on expiry) | Bounds an infinite-loop or output-flood run so it can't hang the caller or blow out memory/log storage capturing stdout/stderr. |

**Network policy detail.** `ExecutionRequest.network` (per-request) or `CODE_EXEC_NETWORK`
(backend default) selects `none` (default) → `--network none`, `egress` →
`--network bridge` (outbound allowed, no host network namespace — an operator-added
egress proxy is expected if this is used), or `host` → `--network host` (escape hatch,
opt-in only, never the default). **Nothing in this codebase sets `CODE_EXEC_NETWORK` to
anything other than its `none` default** — enabling egress or host networking is a
deliberate operator action, not a POLYROB default.

## Defense in depth beyond the sandbox itself

A few of the properties that matter for this tool are enforced *outside* the container,
deliberately, because a container flag can't express them:

- **`git push` / credential-based exfil is NOT a sandbox property.** If an operator opts
  into `CODE_EXEC_NETWORK=egress` (or `host`) and the image has `git` + credentials
  reachable, the container hardening above does not by itself stop a `git push` to an
  attacker-controlled remote — network policy is the only in-sandbox lever, and it
  defaults closed. The actual mitigations live one layer up: (1) `code_execution` is
  **default-OFF** (`CODE_EXEC_ENABLED=false`) and never in a session's default tool list —
  an agent must be explicitly granted it; (2) the separate `git`/`github` write-surface
  tools are gated behind `APPROVAL_REQUIRED_TOOLS` (`git_push`, `github_open_pr`,
  `github_merge_pr` require explicit approval by default —
  `tools/controller/approval.py::DEFAULT_APPROVAL_REQUIRED_TOOLS`); and (3) a delegated
  **leaf** sub-agent can never reach `code_execution`, `git`, `github`, `coding`,
  `process`, `tool_manage`, or `mcp` at all — they're in the leaf tool blocklist
  (`tools/controller/delegation.py::DELEGATE_BLOCKED_TOOLS`), independent of what the
  parent agent has loaded. The sandbox's job is containment of what the code *can touch*;
  exfil-via-credentialed-tool is a capability question, answered by approval + delegation
  policy, not by `docker run` flags.
- **`local_subprocess` is not a sandbox and is refused on servers, by policy, not by
  accident.** It runs code as a plain host subprocess (`capabilities["sandbox"] is
  False`) — a convenience for a single-user local CLI, never for multi-tenant. The guard
  (`tools/code_exec/sandbox_guard.py::code_exec_execution_blocked_reason`) explicitly
  refuses to execute *any* non-sandbox backend unless `POLYROB_LOCAL` is set; on a server,
  `CODE_EXEC_BACKEND=local_subprocess` (or any backend that doesn't set
  `capabilities["sandbox"] = True`) is a hard refusal with a clear error, not a silent
  downgrade. `docker` is the only backend that currently satisfies the guard.
- **Default-OFF end to end.** `CODE_EXEC_ENABLED` defaults `false`
  (`tools/code_exec/__init__.py::code_exec_enabled`); the tool is never registered, let
  alone loaded into a session's `tool_ids`, unless an operator turns it on. Turning it on
  on a server without also having `docker` as the resolved backend is caught by the guard
  above at both registration time (a startup warning) and every `run_code` call (a hard
  refusal).

## Residual risks, stated honestly

- **A container is a namespace/cgroup boundary sharing the host kernel — it is NOT a
  microVM.** `--cap-drop ALL` + `no-new-privileges` + non-root + read-only rootfs close
  the *known, common* container-escape primitives, but a kernel-level 0-day (there is real
  CVE history here — e.g. `runc` CVE-2019-5736, assorted overlayfs/cgroup bugs) can still
  reach the host, because the container and the host share one kernel. A hardware-isolated
  backend (gVisor/Firecracker/E2B/Modal-style microVM) is the actual answer for genuinely
  adversarial multi-tenant code execution. That work was **intentionally scoped out** of
  this deliverable — see `docs/superpowers/plans/2026-07-01-polyrob-coding-first-class/
  02-P1-multitenant-sandboxes-and-code-skills.md` (P1, not yet built) — `docker` is the
  sane hardened default for today's single-tenant-per-run usage, not a claim of
  microVM-grade isolation.
- **The `docker` CLI process itself is trusted-host code and is not further sandboxed.**
  The boundary this document is about is *sandboxed code* vs. *host*; the POLYROB process
  that shells out to `docker run` is the same trust level as the rest of the codebase. In
  particular: the Docker socket (`/var/run/docker.sock`) is never mounted into the
  sandboxed container and `--privileged` is never used — either would be a
  container-escape-by-design and must never be added to `_build_run_argv`.
- **Memory cap without an explicit swap pin.** `--memory` is set; `--memory-swap` is not
  pinned separately. Verified live (`docker inspect`): on a host where swap accounting is
  enabled, Docker defaults `MemorySwap` to **2x** the `--memory` value (e.g. a 1024m
  container can accrete up to ~2048m including swap before OOM-kill), and on a host where
  cgroup swap accounting is disabled entirely the memory cap may not be enforced against
  swap at all. This is a real, if minor, softness in the current flag set — pinning
  `--memory-swap` equal to `--memory` (disabling swap for the container) would close it
  and is a reasonable follow-up.
- **Aggregate concurrency is out of scope for this backend.** `--pids-limit`/`--memory`/
  `--cpus` bound a *single* container's resource use; they say nothing about how many
  `run_code` calls run concurrently across sessions. Host-level exhaustion from many
  simultaneous sandboxed runs is a scheduling/rate-limit concern (sub-agent concurrency
  caps, etc.) layered above this backend, not something these flags alone solve.
- **Image supply chain is an operator responsibility.** The default image
  (`python:3.12-slim`, overridable via `CODE_EXEC_DOCKER_IMAGE`) is pulled from a public
  registry. This backend does not pin a digest, verify a signature, or scan the image —
  operators who need that should point `CODE_EXEC_DOCKER_IMAGE` at a pre-vetted/pinned
  image in their own registry.
- **Non-root ≠ safe from all in-container mischief.** A non-root, capability-dropped,
  read-only, network-denied process can still consume its full CPU/memory/pids budget, or
  attempt (and fail) plenty of things — the flags above bound *impact*, they don't stop an
  adversarial script from trying. That's expected: the goal is containment, not
  behavioral trust.

## How this is verified

`tests/unit/tools/code_exec/test_docker_escape.py` is a live, Docker-daemon-gated
(`@pytest.mark.skipif(shutil.which("docker") is None, ...)`) escape-attempt suite that
exercises every containment property above against a real container: non-root execution,
network-deny-by-default, read-only rootfs (with the tmpfs/workspace exceptions proven
writable), workspace-mount host round-trip, and the PID cap (via a pure argv assertion —
never an actual fork bomb — plus a bounded, fixed-count process-spawn smoke test). Run it
with:

```bash
pytest -q tests/unit/tools/code_exec/test_docker_escape.py
```

If any of those tests fail, treat it as a sandbox regression — not a flaky test — and do
not weaken the assertion to make it pass.
