"""Foreground/background discipline for the shell tool (WS-2).

A foreground command that never returns (a server, a follow, a long sleep, or an
explicit `&`/`nohup`) would hang the turn. `background_nudge` detects those and
returns a message telling the model to pass `background=True` — which routes the
command to the process registry instead of blocking. Pure; no execution.
"""
from __future__ import annotations

import re
from typing import Optional

# Server-launcher command heads that (almost) never return on their own.
_SERVER_PATTERNS = (
    re.compile(r"\bflask\s+run\b"),
    re.compile(r"\b(uvicorn|gunicorn|hypercorn|daphne)\b"),
    re.compile(r"\bhttp\.server\b"),
    re.compile(r"\bmanage\.py\s+runserver\b"),
    re.compile(r"\bnpm\s+(run\s+)?(dev|start|serve)\b"),
    re.compile(r"\b(yarn|pnpm)\s+(dev|start|serve)\b"),
    re.compile(r"\bnext\s+(dev|start)\b"),
    re.compile(r"\bvite\b"),
    re.compile(r"\brails\s+server\b"),
    re.compile(r"\bnode\s+.*server"),
    # a bare `python app.py` / `server.py` / `main.py` is very often a server launch
    re.compile(r"\bpython[0-9.]*\s+\S*(app|server|main|manage|wsgi|asgi)\S*\.py\b"),
    re.compile(r"\b(serve|http-server|caddy|nginx)\b"),
)

# Explicit backgrounding / following that blocks a foreground wait.
_BLOCKING_PATTERNS = (
    re.compile(r"&\s*$"),            # trailing &
    re.compile(r"\bnohup\b"),
    re.compile(r"\btail\b.*\s-[a-zA-Z]*f\b"),   # tail … -f/-F (flags in any order)
    re.compile(r"\btail\b.*--follow\b"),
    re.compile(r"\b(journalctl|kubectl|docker)\b.*(-f\b|--follow\b)"),
    re.compile(r"\bwatch\s"),
    re.compile(r"\bsleep\s+\d{3,}\b"),          # sleep >= 100s
    re.compile(r"\bsleep\s+\d+\s*[mhd]\b"),      # sleep 5m / 2h / 1d
    re.compile(r"\bping\b(?!.*\s-c\b)"),         # ping without a count
)

_NUDGE = (
    "This command looks long-running (a server, a follow, or an explicit "
    "background/&). Re-run it with background=True so it detaches into a managed "
    "job; then use the `process` tool to poll its log / status."
)


def background_nudge(command: str, *, background: bool) -> Optional[str]:
    """Return a nudge string if a foreground command should be backgrounded, else None.

    ``background=True`` always passes (the caller already opted in). Only a
    ``background=False`` command matching a server/blocking pattern is nudged.
    """
    if background:
        return None
    cmd = command or ""
    for pat in _SERVER_PATTERNS + _BLOCKING_PATTERNS:
        if pat.search(cmd):
            return _NUDGE
    return None
