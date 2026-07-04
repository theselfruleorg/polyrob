"""Cross-worker session routing result (roadmap P6).

The routing *decision* a worker makes for an incoming session request: handle it
locally, report which worker owns it (so the caller can forward / return a
meaningful 409 instead of a false 404), or report it missing. Built on the
SQLite registry's cross-process visibility (``exists``/``owner_pid``); actual
request forwarding (HTTP/IPC proxy) is a thin layer on top of this decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

LOCAL = "local"      # owned by this worker; orchestrator is in-process
REMOTE = "remote"    # exists but owned by another worker (owner_pid set)
MISSING = "missing"  # no such active session anywhere


@dataclass(frozen=True)
class SessionRoute:
    status: str                       # LOCAL | REMOTE | MISSING
    orchestrator: Optional[Any] = None  # set only when LOCAL
    owner_pid: Optional[int] = None     # set for LOCAL and REMOTE

    @property
    def is_local(self) -> bool:
        return self.status == LOCAL

    @property
    def is_remote(self) -> bool:
        return self.status == REMOTE

    @property
    def is_missing(self) -> bool:
        return self.status == MISSING
