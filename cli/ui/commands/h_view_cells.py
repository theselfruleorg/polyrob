"""h_view_cells.py — the honest-cell helpers the REPL's table views share.

Extracted from ``handlers.py`` (C42/C44, 2026-09-21): that file is at its size
ratchet (``tests/test_file_size_ratchet.py`` — extract new behaviour into a new
module, never grow the god-file).

Both helpers exist for the same reason: a cell that renders BLANK, or a figure
that disagrees with itself, is indistinguishable from a renderer that failed.
A view may be incomplete; it may not be confident and wrong.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

#: The one "not read / nothing to show" cell in a REPL table.
DASH = "—"


def _context_row(state) -> Optional[tuple]:
    """The ``context`` row of the turn meter, or ``None`` when nothing is honest.

    C44: the three context fields are polled INDEPENDENTLY
    (``cli/ui/state.py::poll`` wraps each in its own ``try``), so a successful
    percent read beside a failed token read rendered ``6% (0/128000)`` — a
    fraction whose numerator contradicts its own percentage. That is worse than
    a missing row, because it looks measured.
    """
    pct = getattr(state, "ctx_percent", 0) or 0
    tokens = getattr(state, "ctx_tokens", 0) or 0
    ctx_max = getattr(state, "ctx_max", 0) or 0
    if tokens > 0 and ctx_max > 0:
        return ("context", f"{pct:.0f}% ({tokens}/{ctx_max})")
    if pct:
        return ("context", f"{pct:.0f}% (token count unread)")
    return None


def _telemetry_row(record: Dict[str, Any]) -> tuple:
    """``(kind, detail, session)`` for one ``/telemetry`` event row (C42).

    An event carrying no outcome/action/reason/preview, and one written by no
    session, both rendered as EMPTY cells. The session id keeps its 12-char
    prefix: that column is a correlation hint, not a handle.
    """
    attrs = record.get("attrs") or {}
    detail = (attrs.get("outcome") or attrs.get("action") or attrs.get("reason")
              or attrs.get("preview") or "")
    session = (record.get("session_id") or "")[:12]
    return (str(record.get("kind") or DASH), str(detail) or DASH, session or DASH)
