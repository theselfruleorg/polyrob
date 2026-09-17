"""Parse a display window token — ``30m`` / ``24h`` / ``7d`` / bare seconds.

Display-only and deliberately fail-open: ``None`` for an unset or malformed
label means "all time" in a heading. The parser that actually BOUNDS a data
query is ``core.recap._parse_window``, which raises on a malformed non-empty
label; a bad label here must never crash a heading. Two REPL handlers carried
this by hand (``/telemetry`` and ``/journey``); it lives here once.
"""
from __future__ import annotations

from typing import Optional


def parse_window_seconds(label: Optional[str]) -> Optional[float]:
    if not label:
        return None
    label = label.strip().lower()
    try:
        if label.endswith("m"):
            return float(label[:-1]) * 60
        if label.endswith("h"):
            return float(label[:-1]) * 3600
        if label.endswith("d"):
            return float(label[:-1]) * 86400
        return float(label)  # bare number = seconds
    except Exception:
        return None
