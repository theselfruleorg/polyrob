"""Read existing delegation receipts without creating or migrating a database."""
import sqlite3
from pathlib import Path

import click


def read_delegations(path, user_id, session_id=None, delegation_id=None):
    path = Path(path)
    if not path.exists():
        return []
    query = "SELECT * FROM delegations WHERE user_id=?"
    args = [user_id]
    for name, value in (("session_id", session_id), ("delegation_id", delegation_id)):
        if value is not None:
            query += f" AND {name}=?"
            args.append(value)
    query += " ORDER BY dispatched_at DESC LIMIT 200"
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, args)]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise click.ClickException(f"Delegation receipts unavailable: {exc}") from exc
