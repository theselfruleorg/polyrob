"""ONE ceiling for the agent's foreground build commands.

`shell_run` (tools/shell/tool.py) clamps its ``timeout`` to this value and the
docker backend's dev-mode ``run_code`` cap follows it when
``CODE_EXEC_MAX_TIMEOUT_SEC`` is unset — the two used to carry separate literals
(120 / 120) that had to be kept aligned by comment.

Publishing & app-deployment evaluation (2026-09-05): every ``tool_timeout`` on
prod was a build command cut mid-stride; a ``pip``/``npm install`` or a
``docker build`` does not fit in 60–120 s. Default 600 s (073 W3, cross-agent parity; was 300); the process-group kill
in the backends is what makes a large ceiling safe.

It is also the ONE resolver for ``CODE_EXEC_MAX_TIMEOUT_SEC``
(:func:`max_timeout_sec`) and ``CODE_EXEC_MAX_OUTPUT_BYTES``
(:func:`max_output_bytes`) — every backend and the kernel read these.

No imports beyond the stdlib and ``core.env`` — both the shell tool and the
code-exec backends import this, and ``tools.shell`` already depends on
``tools.code_exec``.
"""
import math
import os
from typing import Optional

from core.env import int_env

_DEFAULT_DEV_EXEC_MAX_TIMEOUT_SEC = 600.0
#: Confined (non-dev) ``run_code`` cap when CODE_EXEC_MAX_TIMEOUT_SEC is unset.
_DEFAULT_MAX_TIMEOUT_SEC = 30.0
_DEFAULT_MAX_OUTPUT_BYTES = 100_000


def max_output_bytes() -> int:
    """``CODE_EXEC_MAX_OUTPUT_BYTES`` (default 100000); unparsable -> default."""
    return int_env("CODE_EXEC_MAX_OUTPUT_BYTES", _DEFAULT_MAX_OUTPUT_BYTES)


def explicit_max_timeout_sec() -> Optional[float]:
    """The operator's ``CODE_EXEC_MAX_TIMEOUT_SEC``, or None when unset, blank,
    unparsable or non-finite (so the backend's own default applies)."""
    raw = os.getenv("CODE_EXEC_MAX_TIMEOUT_SEC")
    if raw is None or not raw.strip():
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def max_timeout_sec(dev_mode: bool = False) -> float:
    """A backend's ``run_code`` cap. An explicit ``CODE_EXEC_MAX_TIMEOUT_SEC``
    always wins; unset, dev mode follows :func:`dev_exec_max_timeout_sec` and
    confined mode is 30 s."""
    explicit = explicit_max_timeout_sec()
    if explicit is not None:
        return explicit
    return dev_exec_max_timeout_sec() if dev_mode else _DEFAULT_MAX_TIMEOUT_SEC


def dev_exec_max_timeout_sec() -> float:
    """Foreground ceiling in seconds: ``SHELL_MAX_TIMEOUT_SEC`` (default 600).

    Unparseable values fall back to the default; values below 1 clamp to 1 so
    a misconfiguration can never make every command time out instantly.
    """
    raw = os.getenv("SHELL_MAX_TIMEOUT_SEC")
    if raw is None or not raw.strip():
        return _DEFAULT_DEV_EXEC_MAX_TIMEOUT_SEC
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_DEV_EXEC_MAX_TIMEOUT_SEC


def exec_timeout_cap(backend_max: float, ceiling: Optional[float]) -> float:
    """The cap a backend clamps one request to.

    A caller-owned ``ceiling`` (``ExecutionRequest.ceiling`` — ``run_tests``
    passes :func:`dev_exec_max_timeout_sec`) may RAISE the backend's default
    cap, never lower it. An explicit ``CODE_EXEC_MAX_TIMEOUT_SEC`` always wins,
    so an operator's hard cap stays hard (coding-agent review B2: a real test
    suite was cut at the 30 s ``run_code`` default).
    """
    if ceiling is None or explicit_max_timeout_sec() is not None:
        return backend_max
    try:
        return max(backend_max, float(ceiling))
    except (TypeError, ValueError):
        return backend_max
