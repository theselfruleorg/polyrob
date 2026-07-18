"""LSP diagnostics-after-edit (I-2 / H1, dedup decision D2): errors-only,
fail-open, external checkers.

Pure module — no state, no tool coupling, no pip dependency (checkers are
external binaries invoked only if present on PATH). ``diagnose_file`` runs an
external type/lint checker against a freshly-written file and returns a
compact, errors-only diagnostics block, or "" on ANY failure: missing checker,
timeout, unparsable output, or an unsupported extension. It never raises — the
caller (``tools/coding/tool.py``) decides whether to call it at all (gated by
``CODING_LSP_ENABLED`` — see ``core.config_policy.AutonomyConfig.coding_lsp_enabled``).

LANDMINE: NO ``from __future__ import annotations`` anywhere in ``tools/coding/``
(registry param-model introspection landmine on the action-closure module) —
kept consistent here even though this module holds no action closures.
"""
import json
import os
import re
import subprocess

MAX_DIAGNOSTICS_CHARS = 1500

# Extension -> checker name. Anything not listed here is unsupported (no-op).
_CHECKER_BY_EXT = {
    ".py": "pyright",
    ".ts": "tsc",
    ".tsx": "tsc",
    ".js": "tsc",
    ".jsx": "tsc",
}

_TSC_ERROR_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>TS\d+):\s*(?P<message>.+)$"
)


def default_runner(cmd, cwd, timeout_sec):
    """Default ``runner``: invoke an external checker with a wall-clock timeout.

    Captures stdout/stderr as text; ``check=False`` — a checker reporting
    errors exits nonzero by design, that is not a runner failure. Referenced
    as a module attribute (not bound as a parameter default) so tests can
    monkeypatch ``tools.coding.lsp.default_runner`` and have every call inside
    ``diagnose_file`` that doesn't pass its own ``runner`` pick it up.
    """
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout_sec, capture_output=True, text=True, check=False,
    )


def diagnose_file(path: str, root: str, timeout_sec: float = 8.0, runner=None) -> str:
    """Run the extension-appropriate checker against ``path`` (cwd=``root``).

    Returns a compact errors-only diagnostics block (one line per error,
    "path:line:col message"), or "" when there's nothing to report — including
    every failure mode (missing checker binary, timeout, bad output, unknown
    extension). Never raises.
    """
    ext = os.path.splitext(path)[1].lower()
    checker = _CHECKER_BY_EXT.get(ext)
    if checker is None:
        return ""
    run = runner or default_runner
    try:
        if checker == "pyright":
            proc = run(["pyright", "--outputjson", path], root, timeout_sec)
            errors = _parse_pyright(getattr(proc, "stdout", "") or "")
        else:  # tsc
            proc = run(["tsc", "--noEmit", path], root, timeout_sec)
            errors = _parse_tsc(getattr(proc, "stdout", "") or "", getattr(proc, "stderr", "") or "")
    except Exception:
        # Fail-open: FileNotFoundError (missing binary), subprocess.TimeoutExpired,
        # or anything else a checker/runner can throw.
        return ""
    return _cap(errors)


def _parse_pyright(stdout: str) -> list:
    """``severity == "error"`` entries from a ``pyright --outputjson`` payload."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for diag in data.get("generalDiagnostics") or []:
        if not isinstance(diag, dict) or diag.get("severity") != "error":
            continue
        file_path = diag.get("file", "")
        start = ((diag.get("range") or {}).get("start")) or {}
        try:
            line_no = int(start.get("line", 0)) + 1  # pyright ranges are 0-indexed
            col_no = int(start.get("character", 0)) + 1
        except (TypeError, ValueError):
            line_no, col_no = 0, 0
        message = diag.get("message") or ""
        message = message.splitlines()[0] if message else ""
        out.append(f"{file_path}:{line_no}:{col_no} {message}")
    return out


def _parse_tsc(stdout: str, stderr: str) -> list:
    """Lines matching ``tsc``'s ``file(line,col): error TSxxxx: message`` shape."""
    out = []
    for raw in f"{stdout}\n{stderr}".splitlines():
        raw = raw.strip()
        if not raw or "error TS" not in raw:
            continue
        m = _TSC_ERROR_RE.match(raw)
        if m:
            out.append(
                f"{m.group('file')}:{m.group('line')}:{m.group('col')} "
                f"{m.group('code')}: {m.group('message')}"
            )
        else:
            out.append(raw)
    return out


def _cap(lines: list) -> str:
    """Join ``lines`` newline-separated, truncated to fit ``MAX_DIAGNOSTICS_CHARS``
    (reserving room for a trailing "… (+N more)" marker when truncated)."""
    if not lines:
        return ""
    included = []
    for i in range(len(lines)):
        candidate = lines[: i + 1]
        remaining = len(lines) - len(candidate)
        text = "\n".join(candidate)
        if remaining:
            text = f"{text}\n… (+{remaining} more)"
        if len(text) > MAX_DIAGNOSTICS_CHARS:
            break
        included = candidate
    remaining = len(lines) - len(included)
    text = "\n".join(included)
    if remaining:
        text = f"{text}\n… (+{remaining} more)" if text else f"… (+{remaining} more)"
    return text
