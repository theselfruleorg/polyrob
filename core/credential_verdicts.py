"""Process-wide credential verdicts (056 WS4).

A rejected credential is a fact several tiers need without importing each other:
`tools.email_tool` learns of a 535 on login; `agents.task.constants` must stop
REQUESTING the email tool while that rejection is fresh; `core.status_snapshot`
renders it as a health WARN (it reads the durable event instead — core never
imports tools). This tiny register is the shared memory; the durable record is
the `email_auth_rejected` telemetry event the tool also emits.
"""
from __future__ import annotations

import time
from typing import Dict, Tuple

_REJECTIONS: Dict[Tuple[str, str], float] = {}


def record_rejection(kind: str, key: str = "") -> None:
    _REJECTIONS[(kind, key)] = time.monotonic()


def clear_rejection(kind: str, key: str = "") -> None:
    _REJECTIONS.pop((kind, key), None)


def rejected_within(kind: str, seconds: float) -> bool:
    """True while ANY key of ``kind`` was rejected within ``seconds``."""
    now = time.monotonic()
    return any(now - t < seconds for (k, _), t in _REJECTIONS.items() if k == kind)


def _reset_for_tests() -> None:
    _REJECTIONS.clear()
