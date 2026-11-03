"""098 CO-19: the draft revision an owner decision carries from the console.

Kept out of ``webview/pages.py`` (god-file ceiling).
"""
from typing import Optional

from fastapi import Request


async def rev_from_body(request: Request) -> Optional[str]:
    """The optional ``{"rev": "<draft rev>"}`` a decision carries; ``None`` without one."""
    try:
        body = await request.json()
    except Exception:  # no JSON body: the decision carries no rev
        body = None
    return (str(body.get("rev") or "") or None) if isinstance(body, dict) else None
