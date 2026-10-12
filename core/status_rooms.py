"""Status sections: ``apps`` (the durable app service) and ``groups`` (rooms).

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

from core.runtime_paths import surfaces_db_path
import os
import time

from core.status_types import (  # noqa: F401
    HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section, _hhmm, snapshot_rows as _rows,
)


def _apps_section(user_id: str, data_dir: str) -> Section:
    """032: the durable app service — pending approvals lead (CRIT), failures WARN.
    Read-only over app_services.db; an absent registry is "no apps", never created."""
    sec = Section(name="apps")
    db = os.getenv("APP_SERVICES_DB_PATH") or os.path.join(data_dir, "app_services.db")
    if not os.path.exists(db):
        sec.lines.append("no apps")
        sec.data["apps"] = []
        return sec
    rows = _rows(db, "SELECT slug, status, public_url, host_port, last_health, "
                     "last_failure_error FROM app_services WHERE user_id=? ORDER BY created_at",
                 (user_id,))
    sec.data["apps"] = rows
    if not rows:
        sec.lines.append("no apps")
        return sec
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    sec.lines.append(", ".join(f"{n} {s}" for s, n in sorted(by.items())))
    for r in rows:
        where = r.get("public_url") or (f"127.0.0.1:{r['host_port']}" if r.get("host_port") else "-")
        sec.lines.append(f"{r['slug']}: {r['status']} {where}"
                         + (f" (health {_hhmm(r['last_health'])})" if r.get("last_health") else ""))
    pending = [r["slug"] for r in rows if r["status"] == "pending"]
    if pending:
        sec.health.append(HealthItem(
            key="apps_pending", severity=SEVERITY_CRIT,
            text=f"{len(pending)} app(s) waiting on your approval ({', '.join(pending)})",
            remedy="/apps approve <slug> · console → Apps"))
    failed = [r for r in rows if r["status"] == "failed"]
    if failed:
        sec.health.append(HealthItem(
            key="apps_failed", severity=SEVERITY_WARN,
            text=f"{len(failed)} app(s) failed: " + "; ".join(
                f"{r['slug']} ({str(r.get('last_failure_error') or '')[:60]})" for r in failed),
            remedy="/apps logs <slug> · redeploy a fixed tree"))
    return sec




def _groups_section(user_id: str, data_dir: str) -> Section:
    """044 T18: rooms this instance is present in — allowlist + mode + traffic.

    Instance-level, not tenant-scoped: ``group_allowlist.db`` has no
    ``user_id`` column (one bot presence per chat, not per-tenant — a room's
    overlay lives under the OWNER tenant, read via ``chat_policy.load``).
    Read-only, existence-guarded: an absent allowlist is "no rooms", never
    created by this read. ``surfaces.db`` (the room ledger + traffic counters)
    is guarded the SAME way (044 T18 fix round 1, Important 2) — constructing
    ``GroupLedger``/``RoomCaps`` runs their DDL, which would create the file on
    a plain status read; when it is absent every room reports
    ``ledger_rows=0``/``replies_today=0`` with ``note="surfaces.db absent"``
    instead.
    """
    sec = Section(name="groups")
    allow_db = os.path.join(data_dir, "group_allowlist.db")
    if not os.path.exists(allow_db):
        sec.lines.append("no rooms")
        sec.data["rooms"] = []
        return sec
    from core.surfaces.chat_policy import load_for_chat
    from core.surfaces.room_label import render_room_label, room_name
    from core.surfaces.group_allowlist import GroupAllowlist
    rows = GroupAllowlist(allow_db).list_all()
    active = [r for r in rows if r.get("status") == "active"]
    # 044 T21: a room the bot was KICKED from is `left`, not `active`. Named
    # here, or a room that stopped answering reads as a room that never existed.
    left = [{"surface": r["surface"], "chat_id": r["chat_id"]}
            for r in rows if r.get("status") == "left"]
    sec.data["left_rooms"] = left
    left_line = (f"{len(left)} room(s) LEFT (bot removed): "
                 + ", ".join(f"{r['surface']}:{r['chat_id']}" for r in left)) if left else ""
    if not active:
        sec.lines.append("no rooms")
        if left_line:
            sec.lines.append(left_line)
        sec.data["rooms"] = []
        return sec
    surfaces_db = surfaces_db_path(data_dir)
    ledger = caps = None
    if os.path.exists(surfaces_db):
        from core.surfaces.group_ledger import GroupLedger
        from core.surfaces.room_caps import RoomCaps
        ledger = GroupLedger(surfaces_db)
        caps = RoomCaps(surfaces_db)
    now = time.time()
    out_rows = []
    for r in active:
        surface, chat_id = r["surface"], r["chat_id"]
        pol = load_for_chat(data_dir, surface, chat_id)
        if ledger is not None and caps is not None:
            last = ledger.tail(surface, chat_id, limit=1)
            replies_today = caps.replies_since(surface, chat_id, now - 86400)
            ledger_rows = ledger.count(surface, chat_id)
            last_write = last[0].ts if last else None
            note = ""
        else:
            replies_today = 0
            ledger_rows = 0
            last_write = None
            note = "surfaces.db absent"
        out_rows.append({
            "surface": surface, "chat_id": chat_id,
            # 057 WS-D: the NAME is `chat.name` (else `<surface>:<chat_id>`); the
            # owner's allowlist note is a dated LABEL, never the room's title.
            "name": room_name(r, pol),
            "label": render_room_label(r),
            "mode": pol.mode,
            "replies_today": replies_today,
            "ledger_rows": ledger_rows,
            "last_write": last_write,
            "note": note,
        })
    sec.data["rooms"] = out_rows
    sec.lines.append(f"{len(out_rows)} room(s)")
    for row in out_rows:
        line = (f"{row['surface']}:{row['chat_id']} \"{row['name']}\" mode={row['mode']} "
                f"replies_today={row['replies_today']} ledger={row['ledger_rows']} "
                f"last={_hhmm(row['last_write'])}")
        if row.get("label"):
            line += f" {row['label']}"
        if row["note"]:
            line += f" ({row['note']})"
        sec.lines.append(line)
    if left_line:
        sec.lines.append(left_line)
    return sec
