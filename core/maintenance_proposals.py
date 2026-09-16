"""Durable, reviewable proposals for agent-requested self-maintenance.

This module deliberately has no code installer, package manager, git client, or
restart hook. Those effects belong to the trusted CLI/supervisor update path.
The agent can only create an immutable record of the exact bytes it reviewed.
"""
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path, PurePosixPath

from core.security.confined_read import read_confined_bytes
from core.security.confined_write import write_confined_text


_MAX_PROPOSAL_BYTES = 512 * 1024
_HEX = re.compile(r"^[a-f0-9]{64}$")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _relative_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        raise ValueError("proposal path must be a relative POSIX path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("unsafe proposal path")
    return str(parsed)


class MaintenanceProposalStore:
    """Append-only proposal records rooted in the operator data home."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    @classmethod
    def from_runtime(cls):
        from core.runtime_paths import resolve_data_home
        return cls(resolve_data_home() / "maintenance" / "proposals")

    def _write(self, record: dict) -> dict:
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            raise ValueError("maintenance proposal exceeds size limit")
        record["digest"] = _sha256(encoded)
        proposal_id = record["proposal_id"]
        # UUID IDs are generated here, not supplied by the agent. Anchored writer
        # prevents a crafted proposal from traversing or replacing a linked path.
        write_confined_text(self.root / f"{proposal_id}.json", self.root, json.dumps(record, indent=2))
        return record

    def propose_source_patch(self, *, path: str, base_content: str, candidate_content: str,
                             user_id: str = "", session_id: str = "") -> dict:
        relative = _relative_path(path)
        if not isinstance(base_content, str) or not isinstance(candidate_content, str):
            raise ValueError("proposal content must be text")
        if len(base_content.encode("utf-8")) > _MAX_PROPOSAL_BYTES or len(candidate_content.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            raise ValueError("source file exceeds proposal size limit")
        return self._write({
            "version": 1, "proposal_id": uuid.uuid4().hex, "kind": "source_patch",
            "created_at": time.time(), "user_id": str(user_id or ""),
            "session_id": str(session_id or ""), "path": relative,
            "base_sha256": _sha256(base_content), "candidate_sha256": _sha256(candidate_content),
            "candidate_content": candidate_content,
            "activation": "trusted_cli_or_supervisor_only",
            "recovery": "code_only_rollback_after_health_check",
        })

    def propose_dependency(self, *, package: str, user_id: str = "", session_id: str = "") -> dict:
        if not isinstance(package, str) or "==" not in package:
            raise ValueError("dependency proposals require an exact == pin")
        return self._write({
            "version": 1, "proposal_id": uuid.uuid4().hex, "kind": "dependency",
            "created_at": time.time(), "user_id": str(user_id or ""),
            "session_id": str(session_id or ""), "requirement": package,
            "activation": "trusted_cli_or_supervisor_only",
            "recovery": "restore_previous_locked_environment",
        })

    def load(self, proposal_id: str) -> dict:
        if not isinstance(proposal_id, str) or not re.fullmatch(r"[a-f0-9]{32}", proposal_id):
            raise ValueError("invalid proposal id")
        raw = read_confined_bytes(self.root / f"{proposal_id}.json", self.root, _MAX_PROPOSAL_BYTES)
        record = json.loads(raw)
        digest = record.pop("digest", None)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if not isinstance(digest, str) or not _HEX.fullmatch(digest) or digest != _sha256(encoded):
            raise ValueError("altered maintenance proposal")
        return {**record, "digest": digest}
