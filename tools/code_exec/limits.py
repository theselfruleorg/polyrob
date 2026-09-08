"""ONE ceiling for the agent's foreground build commands.

`shell_run` (tools/shell/tool.py) clamps its ``timeout`` to this value and the
docker backend's dev-mode ``run_code`` cap follows it when
``CODE_EXEC_MAX_TIMEOUT_SEC`` is unset — the two used to carry separate literals
(120 / 120) that had to be kept aligned by comment.

Publishing & app-deployment evaluation (2026-09-05): every ``tool_timeout`` on
prod was a build command cut mid-stride; a ``pip``/``npm install`` or a
``docker build`` does not fit in 60–120 s. Default 300 s; the process-group kill
in the backends is what makes a large ceiling safe.

No imports beyond the stdlib — both the shell tool and the code-exec backends
import this, and ``tools.shell`` already depends on ``tools.code_exec``.
"""
import os

_DEFAULT_DEV_EXEC_MAX_TIMEOUT_SEC = 300.0


def dev_exec_max_timeout_sec() -> float:
    """Foreground ceiling in seconds: ``SHELL_MAX_TIMEOUT_SEC`` (default 300).

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
