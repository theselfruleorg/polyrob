"""h_approve.py — the ``/approve`` REPL slash-command handler (owner-UX P2 T4).

Manage the EFFECTIVE approval-gated action set — the same composition
``Controller.__init__`` enforces via
``tools.controller.approval.effective_approval_state`` — from the REPL,
without hand-editing ``preferences.toml`` or restarting the agent. Display can
never drift from enforcement: both call the same helper.

Subcommands: ``list`` (default), ``add <action>``, ``remove <action>``.

- ``list`` shows every currently-gated action name with its source
  (``env (frozen)`` / ``posture`` / ``pref``) plus the effective approval
  provider, and the SAME "gate registered but provider is 'auto'" warning
  ``/config`` shows after a fresh ``approvals.require`` write
  (``h_config.py::_approval_gate_enforcement_warning``).
- ``add <action>`` UNIONs ``<action>`` into the owner's ``approvals.require``
  preference list directly (``write_preference``) — adding a gate only
  TIGHTENS policy, so it needs no owner-review queue (mirrors the "add=safe"
  half of the ``approvals.require`` union-merge contract in
  ``core.prefs.PREF_SCHEMA``). The CLI has no live tool registry to validate
  the action name against, so any non-empty token is accepted with an honest
  "registry check skipped" note.
- ``remove <action>`` can only remove a PREF-added entry — an env/posture
  entry is operator-controlled and explained as such, not removed. Removing a
  pref entry LOOSENS policy, so it queues a ``propose_pref_change`` (Task 3's
  guarded-pref review queue) instead of writing immediately: ``/pending
  approve pref_change approvals.require`` applies it. The proposal is
  OPERATION-based (``op="remove_entry"``, review fix): it stores WHICH entry
  to drop and the promote recomputes against the CURRENT list — a full-list
  snapshot queued at propose time would silently erase any gate the owner
  ADDED between propose and promote.

The core logic is the pure function ``cmd_approve(ctx, args) -> str`` over a
tiny ``ApproveCtx`` dataclass (``user_id``, ``home_dir``) — the SAME pattern
as ``cli/ui/commands/h_config.py``'s ``ConfigCtx``/``cmd_config`` — testable
without a live REPL (see ``tests/unit/cli/test_h_approve.py``); the
registered REPL closure in ``handlers.py`` builds an ``ApproveCtx`` from the
session ``CommandContext`` (mirroring ``/config``'s ``home_dir`` resolution)
and emits the returned string.

Fail-open throughout: this module never raises into the REPL dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from cli.ui import candy


@dataclass
class ApproveCtx:
    """Everything ``/approve`` needs — testable without a live REPL session."""

    user_id: str
    home_dir: Any


def cmd_approve(ctx: ApproveCtx, args: List[str]) -> str:
    """Handle ``/approve list|add|remove`` and return the rendered reply.

    Empty *args* defaults to ``list`` (mirrors ``/config``'s bare-invocation
    default). Never raises — every branch degrades to a friendly string.
    """
    sub = args[0].lower() if args else "list"
    rest = args[1:]
    try:
        if sub == "list":
            return _cmd_list(ctx)
        if sub == "add":
            return _cmd_add(ctx, rest)
        if sub == "remove":
            return _cmd_remove(ctx, rest)
    except Exception as exc:  # fail-open: never crash the REPL dispatcher
        return f"/approve {sub} failed: {exc}"
    return (
        f"unknown /approve subcommand: {sub!r}\n"
        "usage: /approve list | add <action> | remove <action>"
    )


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _provider_warning(provider: str, gate_count: int) -> str:
    """Same warning ``/config`` shows after a fresh ``approvals.require``
    write (``h_config.py::_approval_gate_enforcement_warning``) — a gated
    action is inert unless the resolved provider is something other than
    ``auto`` (allow-all)."""
    if provider == "auto" and gate_count:
        return (
            "\n⚠ gate(s) registered but approval provider is 'auto' (allow-all) — "
            "set approvals.provider to interactive_cli to enforce"
        )
    return ""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(ctx: ApproveCtx) -> str:
    from tools.controller.approval import effective_approval_state

    gates, provider = effective_approval_state(ctx.user_id, ctx.home_dir)
    lines: List[str] = []
    if not gates:
        lines.append(candy.empty("approval gates configured", yet=False))
    else:
        lines.append(f"{len(gates)} approval gate(s):")
        lines.append(candy.kv_lines([(action, f"({gates[action]})") for action in sorted(gates)]))
    lines.append("")
    lines.append(f"provider: {provider}")
    out = "\n".join(lines)
    out += _provider_warning(provider, len(gates))
    return out


# ---------------------------------------------------------------------------
# add <action>
# ---------------------------------------------------------------------------


def _cmd_add(ctx: ApproveCtx, rest: List[str]) -> str:
    if not rest or not rest[0].strip():
        return "usage: /approve add <action>"
    action = rest[0].strip()

    from core.prefs import load_preferences, write_preference

    current = list(load_preferences(ctx.home_dir, ctx.user_id).get("approvals.require", []) or [])
    if action in current:
        return f"'{action}' is already in approvals.require (no change)."
    updated = sorted(set(current) | {action})
    ok, err = write_preference(ctx.home_dir, ctx.user_id, "approvals.require", updated)
    if not ok:
        return f"error: {err}"
    return (
        f"Added '{action}' to approvals.require (gate registered)."
        " Note: this action name is not validated against the live tool"
        " registry — that check is skipped from the CLI context; make sure"
        " it's spelled correctly."
    )


# ---------------------------------------------------------------------------
# remove <action>
# ---------------------------------------------------------------------------


def _operator_controlled_message(action: str, source: str) -> str:
    """Name the actual operator mechanism holding the gate (review fix: a
    posture-sourced gate is NOT 'set in env')."""
    if source == "posture":
        mechanism = "required by AGENT_COMPUTE_POSTURE >= 2"
    else:
        mechanism = "set in env APPROVAL_REQUIRED_TOOLS"
    return f"'{action}' is operator-controlled ({mechanism}) — it can't be removed here."


def _cmd_remove(ctx: ApproveCtx, rest: List[str]) -> str:
    if not rest or not rest[0].strip():
        return "usage: /approve remove <action>"
    action = rest[0].strip()

    from tools.controller.approval import effective_approval_state
    from core.prefs import propose_pref_change

    gates, _provider = effective_approval_state(ctx.user_id, ctx.home_dir)
    source = gates.get(action)
    if source is None:
        return f"'{action}' is not currently gated (nothing to remove)."
    if source != "pref":
        return _operator_controlled_message(action, source)

    # Operation-based removal (review fix): queue WHICH entry to drop; the
    # promote recomputes against the CURRENT list, so a gate added between
    # propose and promote survives (no stale-snapshot clobber).
    ok, result = propose_pref_change(ctx.user_id, "approvals.require", None,
                                     ctx.home_dir, op="remove_entry", entry=action)
    if not ok:
        return f"error: {result}"
    return (
        f"Removing '{action}' from approvals.require loosens policy, so it's "
        f"queued for owner review — /pending approve pref_change {result}"
    )
