"""Thin wrapper around the official `anysite` CLI (pip: anysite-cli).

Pure argv construction (testable without the binary) + an async subprocess
runner with a hard timeout and output cap. No shell — args are passed as a
list, so endpoint/param values can't inject shell syntax. The tool layer owns
ActionResult shaping; this stays transport-only.
"""
import asyncio
import os
import shutil
from dataclasses import dataclass

_MAX_OUTPUT = 200_000  # chars; cap to protect agent context


@dataclass
class AnysiteResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


def _safe_token(s: str) -> str:
    if "\n" in s or "\r" in s or "\x00" in s:
        raise ValueError(f"unsafe character in CLI argument: {s!r}")
    return s


def build_api_argv(endpoint, params=None, output_format="json"):
    """Build the `anysite api …` argv. Pure; no I/O."""
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    argv = ["anysite", "--non-interactive", "api", _safe_token(endpoint)]
    for k, v in (params or {}).items():
        argv.append(f"{_safe_token(str(k))}={_safe_token(str(v))}")
    argv += ["--format", _safe_token(output_format)]
    return argv


def binary_available():
    return shutil.which("anysite") is not None


def ensure_configured():
    """Best-effort: push ANYSITE_API_KEY into the CLI's config once.

    Returns True if the binary is present and a key is available (or already
    configured); False if we can't configure. Never raises.
    """
    key = os.getenv("ANYSITE_API_KEY")
    if not binary_available():
        return False
    if not key:
        return True  # binary present; assume operator configured it out-of-band
    try:
        import subprocess
        subprocess.run(
            ["anysite", "--non-interactive", "config", "set", "api_key", key],
            capture_output=True, timeout=15, check=False,
        )
    except Exception:
        return False
    return True


async def run_anysite(argv, *, timeout=60.0, env=None):
    """Run `anysite …`; capture stdout/stderr, enforce timeout + output cap."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return AnysiteResult(
            stdout=out.decode("utf-8", "replace")[:_MAX_OUTPUT],
            stderr=err.decode("utf-8", "replace")[:_MAX_OUTPUT],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return AnysiteResult(stdout="", stderr="anysite CLI timed out", exit_code=-1, timed_out=True)
