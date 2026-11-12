"""``polyrob doctor --audit-status`` (proposal 116 W10.1).

Builds the doctor's ONE snapshot, then lets ``core.status_audit`` read each
count again with a direct query. Prints every disagreement, and the last
``/status`` text each chat seat sent, so "what did the owner see" has an answer
without a screenshot. Read-only. Exit code 1 when a count differs.
"""
from __future__ import annotations

import json

import click


def audit_status_report(*, as_json: bool = False) -> int:
    from cli._admin_home import admin_data_dir
    from cli.commands.doctor import build_doctor_snapshot
    from core.status_audit import DIFFERS, audit_status, render_audit

    snap, error_line = build_doctor_snapshot()
    if snap is None:
        click.echo(error_line or "status audit: unavailable (the status snapshot "
                                 "could not be built)")
        return 2
    data_dir = admin_data_dir(write=False)
    rows = audit_status(snap.user_id, data_dir, snapshot=snap)
    try:
        from core.surfaces.owner_verb_log import last_status
        seats = last_status(snap.user_id, data_dir=data_dir)
        seats_note = "" if seats else "no /status text recorded yet"
    except Exception as e:      # the record is evidence, not a check
        seats, seats_note = {}, f"unreadable ({type(e).__name__}: {str(e)[:80]})"
    if as_json:
        click.echo(json.dumps({"checks": rows, "last_status": seats,
                               "last_status_note": seats_note}, indent=2, default=str))
    else:
        for line in render_audit(rows):
            click.echo(line)
        if seats_note:
            click.echo(f"Last /status sent: {seats_note}")
        for seat, row in sorted(seats.items()):
            from core.owner_time import stamp
            click.echo(f"Last /status sent on {seat} at {stamp(row.get('ts'))}:")
            for line in str(row.get("text") or "").splitlines():
                click.echo(f"  | {line}")
    return 1 if any(r["result"] == DIFFERS for r in rows) else 0
