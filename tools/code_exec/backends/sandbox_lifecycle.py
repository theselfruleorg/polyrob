"""Shared helpers for a persistent session sandbox (098 TS-08).

A persistent docker container is a ``ResourceLifecycle`` (see ``docker.py``).
These helpers classify a docker-CLI result that says the container itself is
gone (not a failure of the user's code) and build the honest result for it.
"""
from __future__ import annotations

import re

from tools.code_exec.result import ExecutionResult

_DEAD = re.compile(r"no such container|is not running|container .* is restarting", re.I)

RESTARTED_TEXT = (
    "The sandbox container was gone and has been restarted. Your code did NOT run. "
    "Files under /workspace persist; installed packages, running processes and "
    "interpreter state do not. Run the code again.")


def dead_container(code: int, stderr: str) -> bool:
    """True when a ``docker exec`` failed because the container is gone/stopped
    (the docker CLI's own error), never for a non-zero exit of user code."""
    return code != 0 and bool(_DEAD.search(stderr or "")) and code in (1, 125, 126, 127)


def restarted_result(backend: str) -> ExecutionResult:
    return ExecutionResult(stdout="", stderr=RESTARTED_TEXT, exit_code=125, backend=backend)
