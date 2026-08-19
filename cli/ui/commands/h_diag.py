"""`/auth` + `/doctor` — in-session credential and health views (027 WP4).

A user whose key expired mid-session had to quit the REPL to even see the
credential state. Both handlers are read-only renderings of the same oracles
the CLI commands use (`credential_status`, `doctor_report`); key WRITES stay in
the shell (`polyrob auth add <provider>`) so a secret never lands in the
conversation transcript.

No ``from __future__ import annotations`` (consistent with the CLI command
modules).
"""

import os

from cli.ui.commands.registry import CommandContext


def h_doctor(ctx: CommandContext) -> None:
    """Render the full doctor report in-session."""
    from cli.commands.doctor import doctor_report

    try:
        lines = doctor_report(dict(os.environ))
    except Exception as exc:
        ctx.emit(f"doctor unavailable: {exc}", title="doctor")
        return
    ctx.emit("\n".join(lines), title="doctor")


def h_auth(ctx: CommandContext) -> None:
    """Render credential status + the connect grammar in-session."""
    from modules.llm.profiles import credential_status

    try:
        rows = credential_status()
    except Exception as exc:
        ctx.emit(f"auth status unavailable: {exc}", title="auth")
        return

    lines = []
    width = max((len(n) for n in rows), default=8)
    for name, st in rows.items():
        if not getattr(st, "present", False):
            continue
        bits = [getattr(st, "source", "") or "present"]
        if getattr(st, "health", "ok") != "ok":
            bits.append(st.health)
        if not getattr(st, "usable", False):
            bits.append(f"UNUSABLE: {getattr(st, 'reason', '')}")
        lines.append(f"{name.ljust(width)}  {', '.join(bits)}")
    if not lines:
        lines.append("no provider credentials configured")
    absent = [n for n, s in rows.items() if not getattr(s, "present", False)]
    if absent:
        shown, extra = absent[:6], len(absent) - 6
        tail = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"not configured: {', '.join(shown)}{tail}")
    lines.append("connect / replace a key: run `polyrob auth add <provider>` in a shell")
    ctx.emit("\n".join(lines), title="auth")
