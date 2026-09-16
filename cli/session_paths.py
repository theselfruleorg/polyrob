"""Unambiguous lookup for terminal session receipts."""
import re
from pathlib import Path

import click


def session_directory(root, session_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise click.BadParameter("Session IDs contain only letters, numbers, '-' and '_'.")
    root = Path(root)
    matches = set()
    for pattern in (f"*/{session_id}*", f"*/sessions/{session_id}*"):
        matches.update(p for p in root.glob(pattern) if p.is_dir())
    exact = {p for p in matches if p.name == session_id}
    matches = exact or matches
    if len(matches) > 1:
        raise click.ClickException("Ambiguous session ID; use one of: " + ", ".join(str(p.relative_to(root)) for p in sorted(matches)))
    return next(iter(matches), None)
