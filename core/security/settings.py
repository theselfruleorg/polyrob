"""The owner's security settings: ONE typed table (owner decision D1, 2026-10-10).

The rule: **if a check can refuse natural agent work, it is a security SETTING the
owner sets — never a hard-coded refusal.** Every such check reads its row here.
Defaults do not refuse natural work: the key is protected by isolation (a scrubbed
child env, a non-dumpable process, the signer), not by refusing the agent.

A short, enumerated :data:`FLOOR` holds the things that are never natural work
(destroying the machine, writing the agent's own config/wallet/signer state,
reading the seed, a host child while the key is readable). The floor is not
configurable; it is listed so the owner can read every refusal in one place.

Store: ONE env row, ``SECURITY_SETTINGS`` (Access ``file`` in
docs/CONFIGURATION.md) — only the operator env file or the local CLI
(``polyrob config set``) writes it. No agent tool, no console write and no chat
pref can change it. Format: comma-separated ``name=value`` pairs, e.g.
``SECURITY_SETTINGS=host_exec_under_custody=refuse,read_taint=off``. An unknown
name or an invalid value keeps the row's default and is reported in the
``custody`` status section (``polyrob doctor``, ``/status``).

Pure: ``os`` and ``dataclasses`` only (tier-0; every tier reads it).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

#: The ONE env row (Access ``file``).
ENV = "SECURITY_SETTINGS"


@dataclass(frozen=True)
class Setting:
    name: str
    default: str
    choices: Tuple[str, ...]
    description: str
    sites: Tuple[str, ...]


@dataclass(frozen=True)
class FloorRow:
    name: str
    description: str
    sites: Tuple[str, ...]


SETTINGS: Tuple[Setting, ...] = (
    Setting(
        "host_exec_under_custody", "allow", ("allow", "refuse"),
        "Host children while this process holds or can use a wallet key (any signer mode): "
        "git, MCP stdio servers, lazy installs, coding snapshots and LSP checks, self_env, "
        "@diff, local run_code and the posture-3 host shell. `allow`: they run with a "
        "scrubbed env (no seed, no API keys). `refuse`: they refuse; use a sandbox backend.",
        ("core/security/host_execution.py::host_execution_refusal",
         "tools/code_exec/sandbox_guard.py::require_sandbox_or_none",
         "tools/code_exec/sandbox_guard.py::remote_shell_refusal")),
    Setting(
        "packs_under_custody", "allow", ("allow", "refuse"),
        "A third-party code pack the owner named in POLYROB_PACKS, under wallet custody or "
        "remote signing. A pack runs INSIDE the agent process. `allow`: it loads. "
        "`refuse`: it is refused at load and at `polyrob pack install`.",
        ("core/packs/loader.py::custody_refusal",)),
    Setting(
        "unattended_dangerous_shell", "sandbox", ("sandbox", "refuse"),
        "A shell line the guard classes dangerous (`bash -c`, `python - <<EOF`, a recursive "
        "delete) in an unattended run (goal, cron), where no owner can answer an approval. "
        "`sandbox`: it runs when the command runs in a sandbox container, never on the host. "
        "`refuse`: it is refused.",
        ("tools/controller/command_guard_hook.py::make_command_guard_hook",
         "tools/controller/command_guard_hook.py::make_shell_command_exemption")),
    Setting(
        "refusal_taint", "hold", ("hold", "off"),
        "After a money verb is refused in a run, that run's public sends (posts, group and "
        "room messages, public cron delivery) go to the owner instead. `off`: no hold.",
        ("core/security/refusal_taint.py::is_tainted",)),
    Setting(
        "read_taint", "on", ("on", "off"),
        "After a turn reads third-party content, its writes are agent-authored "
        "(restrict-only) and owner-only verbs queue an owner tap. `off`: the owner turn "
        "keeps its authorship after a read.",
        ("core/security/read_taint.py::is_tainted",
         "core/security/read_taint.py::orchestrator_tainted")),
    Setting(
        "financial_claim_gate", "on", ("on", "off"),
        "An outbound message on any rail (chat, email, X API and X browser posts/DMs) "
        "whose text claims money moved is refused unless a settled record backs it. "
        "`off`: no claim check. (The older FINANCIAL_CLAIM_GATE_ENABLED=false also turns "
        "it off.)",
        ("tools/controller/financial_claim_gate.py::gate_enabled",)),
)

FLOOR: Tuple[FloorRow, ...] = (
    FloorRow("shell_destroy_machine",
             "deletes a root, home or system tree; formats or writes a raw disk; a fork "
             "bomb; kills every process",
             ("core/security/command_guard.py::_FLOOR_RAW", "core/security/command_guard.py::_rm_verdict",
              "core/security/command_guard.py::_perm_verdict")),
    FloorRow("shell_root_pipe", "pipes a download into a root shell",
             ("core/security/command_guard.py::_FLOOR_RAW",)),
    FloorRow("agent_state_write",
             "ONE command segment (or a redirect target) writes the agent's own config, "
             "wallet or signer state: /etc/polyrob, polyrob.env, ~/.polyrob/.env, wallet/, "
             "signer/, keys/, /run/polyrob-signer",
             ("core/security/command_guard.py::_state_write_floor",)),
    FloorRow("seed_read",
             "a command line names a wallet seed variable together with an env file or an "
             "env dump (it would put the seed on screen or in a file)",
             ("core/security/command_guard.py::_SEED_NAME_RE",)),
    FloorRow("agent_service_stop", "stops or kills the agent's own service or process",
             ("core/security/command_guard.py::_FLOOR_RAW",)),
    FloorRow("key_isolation_gap",
             "a host child while this process holds a wallet key that a same-UID child "
             "could read (/proc/<pid>/environ open: the non-dumpable hardening failed)",
             ("core/security/host_execution.py::custody_isolation_gap",)),
)

_BY_NAME: Dict[str, Setting] = {s.name: s for s in SETTINGS}


def _parse(raw) -> Tuple[Dict[str, str], List[str]]:
    values: Dict[str, str] = {}
    invalid: List[str] = []
    for part in str(raw or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, val = part.partition("=")
        name, val = name.strip().lower(), val.strip().lower()
        row = _BY_NAME.get(name)
        if not sep or row is None or val not in row.choices:
            invalid.append(part)
            continue
        values[name] = val
    return values, invalid


def value(name: str) -> str:
    """The effective value of row *name* (KeyError for a name not in the table)."""
    row = _BY_NAME[name]
    values, _ = _parse(os.environ.get(ENV))
    return values.get(name, row.default)


def invalid_entries() -> List[str]:
    """The ``SECURITY_SETTINGS`` entries that were ignored (unknown name / bad value)."""
    return _parse(os.environ.get(ENV))[1]


def effective() -> List[dict]:
    """One dict per row: name, value, default, choices, source (owner|default)."""
    values, _ = _parse(os.environ.get(ENV))
    return [{"name": r.name, "value": values.get(r.name, r.default), "default": r.default,
             "choices": list(r.choices),
             "source": "owner" if r.name in values else "default"} for r in SETTINGS]


def status_line() -> str:
    """``security settings: name=value · …`` — an owner-set row is marked ``(owner)``."""
    parts = [f"{r['name']}={r['value']}" + (" (owner)" if r["source"] == "owner" else "")
             for r in effective()]
    return "security settings: " + " · ".join(parts)


def refusal_note(name: str) -> str:
    """The clause a refusal adds so the owner knows which row refused and how to change it."""
    return (f"the owner's security setting {ENV} {name}={value(name)} refuses it "
            f"(the owner changes it in the env file or with `polyrob config set {ENV} …`)")


__all__ = ["ENV", "Setting", "FloorRow", "SETTINGS", "FLOOR", "value", "invalid_entries",
           "effective", "status_line", "refusal_note"]
