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


# Scrubbed subprocess env for the external checker: an allowlist mirroring
# tools/coding/snapshot.py::_env / tools/git/tool.py — PATH/HOME/LANG/LC_ALL only.
# A static checker (pyright/tsc) needs none of our secrets, and passing the full
# os.environ would hand every ``*_API_KEY``/token to a checker plugin/config
# living in the workspace. Allowlist, not blocklist, so a new secret env var is
# excluded by default.
_CHECKER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")


def _checker_env() -> dict:
    return {k: os.environ[k] for k in _CHECKER_ENV_KEYS if k in os.environ}


def default_runner(cmd, cwd, timeout_sec):
    """Default ``runner``: invoke an external checker with a wall-clock timeout.

    Captures stdout/stderr as text; ``check=False`` — a checker reporting
    errors exits nonzero by design, that is not a runner failure. Referenced
    as a module attribute (not bound as a parameter default) so tests can
    monkeypatch ``tools.coding.lsp.default_runner`` and have every call inside
    ``diagnose_file`` that doesn't pass its own ``runner`` pick it up. Runs with
    a scrubbed env (:func:`_checker_env`) so secrets never reach the checker.
    """
    from core.security.host_execution import host_execution_refusal
    refusal = host_execution_refusal()
    if refusal:
        raise RuntimeError(refusal)
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout_sec, capture_output=True, text=True, check=False,
        env=_checker_env(),
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
            cmd = _tsc_command(path, root, ext)
            if cmd is None:
                return ""
            proc = run(cmd, root, timeout_sec)
            errors = _parse_tsc(getattr(proc, "stdout", "") or "", getattr(proc, "stderr", "") or "")
            if "-p" in cmd:
                errors = _only_for(errors, path, root)
    except Exception:
        # Fail-open: FileNotFoundError (missing binary), subprocess.TimeoutExpired,
        # or anything else a checker/runner can throw.
        return ""
    return _cap(errors)


def _find_tsconfig(path: str, root: str):
    """The nearest ``tsconfig.json`` from *path*'s directory up to *root*, or None."""
    here = os.path.dirname(os.path.abspath(os.path.join(root, path)))
    top = os.path.abspath(root)
    # Containment BEFORE any probe: never stat a config outside the workspace.
    while here == top or here.startswith(top + os.sep):
        candidate = os.path.join(here, "tsconfig.json")
        if os.path.isfile(candidate):
            return candidate
        if here == top:
            return None
        here = os.path.dirname(here)
    return None


def _tsc_command(path: str, root: str, ext: str):
    """The tsc argv for *path*, or None when no check would be honest.

    Coding-agent review B10 (2026-09-24): ``tsc --noEmit <file>`` IGNORES the
    project's ``tsconfig.json`` (paths, jsx, lib, strictness), so it reported
    errors the project does not have; on a ``.js`` file it reported TS6504
    ("did you mean allowJs") as an error. Under a tsconfig, check the PROJECT
    and keep only this file's errors. Without one, a ``.js``/``.jsx`` file has
    no type contract to check, and a ``.tsx`` file needs ``--jsx preserve``.
    """
    tsconfig = _find_tsconfig(path, root)
    if tsconfig:
        return ["tsc", "--noEmit", "--pretty", "false", "-p", tsconfig]
    if ext in (".js", ".jsx"):
        return None
    cmd = ["tsc", "--noEmit", "--pretty", "false"]
    if ext == ".tsx":
        cmd += ["--jsx", "preserve"]
    return cmd + [path]


_LOCATED_RE = re.compile(r"^(?P<file>.+?):\d+:\d+ ")


def _only_for(errors: list, path: str, root: str) -> list:
    """Project mode: keep *path*'s own ``file:line:col ...`` lines and every
    UNLOCATED compiler/config error (a broken tsconfig is the project's real
    problem, not noise); drop other files' findings. The file part is matched
    up to ``:line:col`` so a Windows drive letter (``C:\\x.ts``) survives."""
    want = os.path.realpath(os.path.join(root, path))
    kept = []
    for line in errors:
        m = _LOCATED_RE.match(line)
        if m is None or os.path.realpath(os.path.join(root, m.group("file"))) == want:
            kept.append(line)
    return kept


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
            # An unlocated "error TSxxxx" is a compiler/config error: real, so
            # reported (the .js TS6504 false positive is gone because a .js
            # file with no tsconfig is not checked at all).
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
