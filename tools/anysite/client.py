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
    # argv[0] is RESOLVED, not a bare name: the venv's bin dir is not on a
    # systemd unit's PATH (see `binary_path`), so exec-ing "anysite" fails on a
    # box where the CLI is installed.
    argv = [binary_path() or "anysite", "--non-interactive", "api",
            _safe_token(endpoint)]
    for k, v in (params or {}).items():
        argv.append(f"{_safe_token(str(k))}={_safe_token(str(v))}")
    argv += ["--format", _safe_token(output_format)]
    return argv


def build_describe_argv(endpoint=None, search=None):
    """Build the `anysite describe …` argv (endpoint DISCOVERY). Pure; no I/O.

    `search` → paths-only JSON match list (a keyword can hit dozens of
    endpoints; quiet keeps the agent's context small). `endpoint` → the FULL
    schema for that one endpoint (input params + output fields) — the agent
    reads the exact param names/types before calling `anysite api`.

    2026-09-15 (owner rail "fix Anysite tool"): the CLI always had this and the
    tool never exposed it, so agents guessed endpoint paths and burned steps on
    Not Found / param-validation churn.
    """
    argv = [binary_path() or "anysite", "--non-interactive", "describe"]
    if search:
        argv += ["--search", _safe_token(str(search)), "--json", "--quiet"]
        return argv
    argv.append(_safe_token(str(endpoint)))
    argv.append("--json")
    return argv


#: Env names that carry the credential, in order. `ANYSITE_API_KEY` is what this
#: codebase has always read; `ANYSITE_ACCESS_TOKEN` is what AnySite's own docs and
#: env templates call it (their REST auth header is literally `access-token`), and
#: an operator who copies their template gets a key the agent could not see.
#: Reading both costs nothing and removes a silent-dark failure.
_KEY_ENV_NAMES = ("ANYSITE_API_KEY", "ANYSITE_ACCESS_TOKEN")


def api_key():
    """The configured credential, or None."""
    for name in _KEY_ENV_NAMES:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def binary_path():
    """Absolute path to the `anysite` console script, or None.

    ⚠️ ``shutil.which`` alone is NOT enough, and prod proved it. `anysite-cli` is a
    declared dependency, so pip puts its console script in the SAME directory as
    the running interpreter — ``/opt/polyrob/venv/bin/anysite``. A systemd unit
    execs the venv's python directly and inherits the system PATH, which does not
    contain that directory, so ``which("anysite")`` returned None on a box where
    the binary was installed and working. The tool then reported "anysite CLI not
    available — install with pip install anysite-cli" about a package that was
    already there.
    """
    import sys
    here = os.path.dirname(os.path.abspath(sys.executable))
    for candidate in (os.path.join(here, "anysite"),
                      os.path.join(here, "anysite.exe")):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("anysite")


def binary_available():
    return binary_path() is not None


def missing_requirement():
    """Which half is absent — ``"binary"``, ``"key"`` or ``None``.

    Two failures with one message taught the operator to check the wrong thing.
    """
    if not binary_available():
        return "binary"
    if not api_key():
        return "key"
    return None


def ensure_configured():
    """Best-effort: push the credential into the CLI's config once.

    Returns True only when the binary is present AND a key is configured. The
    previous version returned True for "binary present, no key" on the assumption
    the operator had configured it out of band — which turned a missing credential
    into a call that fails later with the vendor's error instead of ours.
    """
    key = api_key()
    path = binary_path()
    if not path or not key:
        return False
    try:
        import subprocess
        # The key stays in argv here because `config set <name> <value>` is the
        # CLI's only write path for it — there is no env-var form of the write.
        # The ENV is scrubbed regardless (S2): argv exposure of this one key to
        # `ps` is a far smaller hole than handing the CLI the wallet seed.
        subprocess.run(
            [path, "--non-interactive", "config", "set", "api_key", key],
            capture_output=True, timeout=15, check=False,
            env=build_anysite_env(),
        )
    except Exception:
        return False
    return True


def build_anysite_env(extra=None):
    """Scrubbed environment for an `anysite` child process (S2).

    Was ``{**os.environ, **(env or {})}`` — the CLI (a third-party package the
    agent can drive against arbitrary sites) inherited AGENT_WALLET_MASTER_SEED
    and every provider API key. Now: the shared allowlist policy, plus the
    anysite credential injected EXPLICITLY.

    The explicit re-add is the same call the MCP policy makes
    (``tools/mcp/child_env.py``): the shared policy strips every secret-NAMED
    var, which is right for ambient inheritance and wrong for the one credential
    this child is supposed to have. Passing it by env is also what lets the
    argv form stay a fallback rather than the only channel.
    """
    from tools.code_exec.env_policy import build_child_env
    env = build_child_env(
        extra or {},
        # Proxy settings: the CLI is an HTTP client and a proxied box cannot
        # reach anysite without them. No POLYROB credential can live here.
        extra_allowlist=("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
                         "http_proxy", "https_proxy", "no_proxy", "all_proxy",
                         "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "APPDATA",
                         "LOCALAPPDATA", "USERPROFILE", "SystemRoot",
                         "SYSTEMROOT", "COMSPEC", "PATHEXT"),
    )
    key = api_key()
    if key:
        # Both names the CLI/vendor may read (see _KEY_ENV_NAMES).
        for name in _KEY_ENV_NAMES:
            env[name] = key
    return env


async def run_anysite(argv, *, timeout=60.0, env=None):
    """Run `anysite …`; capture stdout/stderr, enforce timeout + output cap."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_anysite_env(env),
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
