"""Durable, append-only audit sink for the wallet PolicyGate (G3).

An in-memory ``list`` of audit entries is lost on restart, so the harness can't sum
lifetime/rolling spend across a service restart — a mainnet prerequisite. This sink
is a ``list`` subclass that mirrors every appended entry to an append-only JSONL file
and reloads prior entries on construction, so a fresh ``PolicyGate(audit_sink=...)``
sees the full history. Default behavior is unchanged: PolicyGate uses a plain list
unless a sink is injected (only the factory does, when the wallet is enabled).

Fail-open: a file I/O error never blocks an action (the in-memory copy is always
authoritative for the live process); persistence is best-effort.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List

logger = logging.getLogger(__name__)


class JsonlAuditSink(list):
    """A list of audit dicts mirrored to an append-only JSONL file."""

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        list.append(self, json.loads(line))  # base append: no re-write
                    except json.JSONDecodeError:
                        continue  # skip a corrupt line, keep the rest
        except OSError as e:
            logger.warning("wallet audit sink load failed (%s): %s", self._path, e)

    def append(self, entry: dict) -> None:  # type: ignore[override]
        list.append(self, entry)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("wallet audit sink write failed (%s): %s", self._path, e)


def default_audit_sink(data_dir: str = "data") -> List[dict]:
    """The factory's default persistent sink at ``<data_dir>/wallet/audit.jsonl``."""
    return JsonlAuditSink(os.path.join(data_dir, "wallet", "audit.jsonl"))
