"""The two helpers every keyless provider carried by hand.

``get_json`` is one urllib GET with the project user-agent and a bounded
timeout; ``parse_int`` is the "a malformed number is None (unknown), never
0" rule — a zero would read as a free bridge, a free trade or a zero floor.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Optional

USER_AGENT = "polyrob-defi/1.0"


def get_json(url: str, *, timeout: float, user_agent: str = USER_AGENT) -> Any:
    req = urllib.request.Request(url, headers={"accept": "application/json",
                                               "user-agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Provider numbers arrive as decimal STRINGS (or ints). A malformed one
    is *default* (None = unknown), never 0."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
