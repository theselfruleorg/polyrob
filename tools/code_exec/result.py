"""Code-execution request/result types (Item 3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ExecutionRequest:
    """A single code-execution request."""
    language: str
    code: str
    stdin: Optional[str] = None
    timeout: Optional[float] = None
    workdir: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    # P0 (Task 1): per-request network policy. None = backend default (CODE_EXEC_NETWORK);
    # otherwise "none" | "egress" | "host". The local_subprocess backend IGNORES this (it
    # always has host network); the DockerBackend interprets it (Task 3).
    network: Optional[str] = None
    # WS-1 (compute posture): posture-gated sandbox-dev mode. True ONLY when the caller
    # passed compute_posture_allows(ctx, 1) — the DockerBackend then mounts a writable
    # /install, runs `python -s` with PYTHONPATH=/install (instead of the env-ignoring
    # `python -I`) and sets HOME=/workspace + PIP_TARGET=/install so pip installs are
    # importable. False (default) = byte-identical hardened posture-0 path.
    dev_mode: bool = False


@dataclass
class ExecutionResult:
    """Captured outcome of a code execution."""
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    truncated: bool = False
    duration_sec: float = 0.0
    backend: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
