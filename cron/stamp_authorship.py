"""One-time, idempotent: stamp ``payload.authored_by`` on every UNSTAMPED cron row.

Why (verifier round 3, 2026-10-08): standing owner authority (the rig as
written, an X post or room moderation without a per-run approval, a write
verb, a trusted buy target) now reads a POSITIVE ``authored_by == owner``
stamp (``core.config_policy.rigs.is_owner_authored``). An unstamped row used to
count as the owner's, but:

* ``cronjob_schedule`` (the agent's tool) stamps ``authored_by`` only since
  ``d2ef4acff`` (committed 2026-09-23, first on prod with 9bf0a7491 at
  2026-09-23 17:41Z). An unstamped row OLDER than that may be the agent's.
* Every unstamped row NEWER than that came from an owner path that did not
  stamp yet (``polyrob cron schedule``/``digest``, ``/cron add`` through
  ``core.owner_create.create_cron``, the console, ``/groups service``, the
  operator seeders) — all of them stamp ``owner`` now.

So: ``created_at >= cutoff`` -> ``authored_by=owner``; older ->
``authored_by=agent`` + ``legacy_unstamped=true`` (``/adopt`` lists it and the
owner confirms it after seeing what it does). A row that already carries
``authored_by`` is never touched, so a second run changes nothing.

cron.db is not one of the semver-migrated databases (``migrations/`` runs on
``bot.db``), so this is a module with a dry run::

    python -m cron.stamp_authorship --dry-run          # print the plan only
    python -m cron.stamp_authorship                    # apply it
    python -m cron.stamp_authorship --db /var/lib/polyrob/data/cron.db --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional

#: First prod deploy carrying ``d2ef4acff`` (9bf0a7491, 2026-09-23 17:41Z),
#: rounded up. Server timestamps are naive UTC (``datetime.now`` on a UTC box).
DEFAULT_CUTOFF = "2026-09-23T18:00:00"
LEGACY_KEY = "legacy_unstamped"


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


def plan(db_path: str, cutoff: str = DEFAULT_CUTOFF) -> List[Dict]:
    """One entry per unstamped row: ``{id, user_id, status, created_at, task,
    author, legacy}``. Reads only."""
    edge = _parse(cutoff)
    if edge is None:
        raise ValueError(f"bad cutoff {cutoff!r} (ISO date/time)")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, user_id, task, status, enabled, payload, "
                           "created_at FROM cron_jobs ORDER BY created_at").fetchall()
    finally:
        con.close()
    out: List[Dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            out.append({"id": r["id"], "user_id": r["user_id"], "status": r["status"],
                        "enabled": bool(r["enabled"]), "created_at": r["created_at"],
                        "task": r["task"], "author": None, "legacy": False,
                        "skip": "payload is not a JSON object"})
            continue
        if str(payload.get("authored_by") or "").strip():
            continue
        created = _parse(r["created_at"])
        new = created is not None and created >= edge
        out.append({"id": r["id"], "user_id": r["user_id"], "status": r["status"],
                    "enabled": bool(r["enabled"]), "created_at": r["created_at"],
                    "task": r["task"], "author": "owner" if new else "agent",
                    "legacy": not new, "skip": None})
    return out


def apply(db_path: str, entries: List[Dict]) -> int:
    """Stamp each planned row. Compare-and-set on an unstamped payload, so a row
    stamped meanwhile (or by a second run) is left alone. Returns rows written."""
    con = sqlite3.connect(db_path, timeout=30)
    written = 0
    try:
        for e in entries:
            if e.get("skip") or not e.get("author"):
                continue
            row = con.execute("SELECT payload FROM cron_jobs WHERE id=?", (e["id"],)).fetchone()
            if row is None:
                continue
            payload = json.loads(row[0] or "{}")
            if not isinstance(payload, dict) or str(payload.get("authored_by") or "").strip():
                continue
            payload["authored_by"] = e["author"]
            if e.get("legacy"):
                payload[LEGACY_KEY] = True
            cur = con.execute("UPDATE cron_jobs SET payload=? WHERE id=? AND payload=?",
                              (json.dumps(payload), e["id"], row[0]))
            written += cur.rowcount
        con.commit()
    finally:
        con.close()
    return written


def _render(entries: List[Dict], cutoff: str) -> str:
    lines = [f"cutoff {cutoff}: unstamped rows created at/after it -> owner; "
             f"older -> agent + {LEGACY_KEY} (list them with /adopt)"]
    if not entries:
        lines.append("nothing to stamp — every row carries authored_by")
    for e in entries:
        task = " ".join(str(e.get("task") or "").split())[:70]
        live = "enabled" if e.get("enabled") else "disabled"
        what = f"SKIP ({e['skip']})" if e.get("skip") else (
            "owner" if e["author"] == "owner" else f"agent + {LEGACY_KEY}")
        lines.append(f"  {e['id']}  {e.get('created_at') or '?':<26} {e['status']:<10} "
                     f"{live:<8} -> {what:<26} {task}")
    n_owner = sum(1 for e in entries if e.get("author") == "owner" and not e.get("skip"))
    n_agent = sum(1 for e in entries if e.get("author") == "agent" and not e.get("skip"))
    lines.append(f"{len(entries)} unstamped row(s): {n_owner} -> owner, {n_agent} -> agent (legacy)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cron.stamp_authorship",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="cron.db path (default: the data home's)")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help=f"ISO time; unstamped rows at/after it are the owner's (default {DEFAULT_CUTOFF})")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args(argv)
    db = args.db
    if not db:
        from core.runtime_paths import cron_db_path
        db = cron_db_path()
    try:
        entries = plan(db, args.cutoff)
    except (sqlite3.Error, ValueError) as exc:
        print(f"cannot read {db}: {exc}", file=sys.stderr)
        return 2
    print(f"cron db: {db}")
    print(_render(entries, args.cutoff))
    if args.dry_run:
        print("dry run — nothing written")
        return 0
    n = apply(db, entries)
    print(f"stamped {n} row(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
