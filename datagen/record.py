"""Canonical trajectory record (schema v1) — design spec §A1.

One record per session: the persisted message history, the step-level agent
ledger, aggregated LLM usage, and quality labels (RunOutcome/episode-sourced).
Renderers in ``datagen.formats`` turn this into training formats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = 1

#: Labels every record carries; enriched from an episodes row or a RunOutcome.
DEFAULT_LABELS = {
    "outcome": "unknown",            # done | failed | partial | cancelled | unknown
    "verified": "unverified",        # verified | unverified | failed_verification
    "refusal": False,
    "all_actions_errored": False,
    "steps": 0,
    "spend_usd": 0.0,
}


@dataclass
class TrajectoryRecord:
    session_id: str
    user_id: str = ""
    instance_id: str = ""
    created_at: Optional[str] = None
    exported_at: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    task: Optional[str] = None
    messages: list = field(default_factory=list)   # message_history.json "messages"
    steps: list = field(default_factory=list)      # agent_history "history" entries
    labels: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)      # aggregated llm_usage
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **self.__dict__}
