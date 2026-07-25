"""SSH execution backend (T2.7) — runs code on a remote host over the system
``ssh`` binary. Third ``ExecutionBackend`` alongside ``local_subprocess`` and
``docker``.

⚠️ HONESTY (the load-bearing design decision, see ``SANDBOX_SECURITY.md``): a
generic remote host is a DIFFERENT trust domain from a hardened container — agent
code runs with the SSH user's full privileges there. ``capabilities["sandbox"]``
is therefore **False by default**; it flips to True ONLY when the operator sets
``CODE_EXEC_SSH_SANDBOXED=true`` as an explicit attestation that the remote is
hardened/disposable. The sandbox guard (``tools/code_exec/sandbox_guard.py``)
refuses this backend on a server unless attested — that refusal is correct, not
a bug.

Transport: shells out to ``/usr/bin/ssh`` (paramiko/asyncssh are NOT dependencies)
via the same pattern as the docker backend's persistent-mode runner
(``subprocess.Popen(..., start_new_session=True)`` in a thread executor, so it
never touches ``asyncio.create_subprocess_exec``'s non-main-thread child-watcher
hazard — see ``docker.py::_default_docker_runner``). All real invocation goes
through the injectable ``ssh_runner`` seam (mirrors ``DockerRunner``), so every
behavioural test is hostless via a fake runner; ``_build_ssh_argv`` /
``_build_remote_command`` are PURE (env-read only, no subprocess) so argv/quoting
shape is unit-testable without any runner at all.

Ephemeral-only (v1): every ``run()`` is its own ``ssh`` invocation — no persistent
remote shell/session survives between calls (unlike the docker backend's opt-in
persistent per-session mode). ``resolve_backend``'s docker-only session/dev branch
is deliberately NOT extended to ssh.

Quoting (empirically verified against a real local ``sshd``, not just the man
page — see the T2.7 Task-1 report): the remote command is assembled as ONE
string — ``[env K=V ...] timeout --signal=KILL <n> python3 -c <quoted>`` (python)
or ``... bash -c <quoted>`` (bash) — with every dynamic piece run through
``shlex.quote`` (POSIX single-quoting). It is passed to ``ssh`` as a SINGLE
trailing argv element, and the destination (``target``) itself is preceded by a
literal ``--`` separator token — ``ssh <opts> -- <target> <wrapped command>``.
Three properties this buys:

1. Local safety: ``subprocess.Popen`` always receives an argv LIST, never
   ``shell=True`` — the code is never interpreted by a local shell, so nothing in
   it (``$(...)``, backticks, quotes, newlines) can affect the ssh invocation
   itself.
2. Remote safety: ``shlex.quote`` produces a POSIX-safe single-quoted token, so a
   ``$(...)``/backtick/``"..."`` embedded in the code stays INERT to the remote
   shell — it is handed to ``python3 -c``/``bash -c`` as one literal string
   argument, not interpreted as shell syntax.
3. Option-injection safety (review fix, T2.7): ``--`` is placed BEFORE
   ``target`` (never after) so OpenSSH's own argument parser stops treating any
   later token — including ``target`` — as a flag, even if an operator's
   ``CODE_EXEC_SSH_HOST``/``CODE_EXEC_SSH_USER`` value is attacker-influenced and
   begins with ``-`` (e.g. ``-oProxyCommand=...``). Putting ``--`` AFTER
   ``target`` (the pre-fix shape) does NOT help — OpenSSH has already parsed
   ``target`` as an option by the time it sees the separator, so a
   ``-``-prefixed host/user achieves arbitrary LOCAL command execution before
   the ssh client ever contacts the remote host. As belt-and-suspenders,
   ``_build_ssh_argv`` ALSO refuses to build an invocation at all when the host
   or user component begins with ``-`` (``ExecutionBackendError``), so a
   misconfigured/attacker-influenced value is rejected honestly rather than
   relying on ``--`` placement alone.

Holds no ``@BaseTool.action`` closures — ``from __future__ import annotations`` is
safe.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from core.env import bool_env as _bool_env, float_env as _float_env, int_env as _int_env
from tools.code_exec.backend import ExecutionBackend, ExecutionBackendError
from tools.code_exec.env_policy import SECRET_PAT, build_child_env
from tools.code_exec.result import ExecutionRequest, ExecutionResult

logger = logging.getLogger(__name__)

_PY = ("python", "python3", "py")
_SH = ("bash", "sh", "shell")

#: Same exit-code convention as the docker backend's in-container `timeout
#: --signal=KILL` wrapper (docker.py: 124 = coreutils' synthetic "I killed it"
#: exit; 137 = 128+SIGKILL, what coreutils actually reports when the signal IS
#: KILL). Both are treated as `timed_out=True` here.
_TIMEOUT_EXIT_CODES = frozenset({124, 137})

#: A remote env var NAME must be a valid POSIX shell identifier to be safely
#: emitted as `env NAME=value ...` — anything else is silently dropped (never a
#: shell-syntax escape hatch).
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: (full argv incl. leading "ssh", *, input=<stdin text|None>, timeout=<seconds|None>)
#: -> (returncode, stdout_text, stderr_text). Injectable so every behavioural test
#: needs no real `ssh` binary or network (see `test_ssh_backend.py`'s `_FakeSsh`).
SshRunner = Callable[..., Awaitable[Tuple[int, str, str]]]


class _SshRunnerTimeout(Exception):
    """Raised by ``_default_ssh_runner`` when the local ``ssh`` CLI client itself
    hangs past its host-side backstop. Only the DEFAULT runner ever raises this —
    an injected fake runner never does (tests simulate timeouts via the mapped
    124/137 exit codes instead, matching the real remote `timeout --signal=KILL`
    wrapper firing)."""


async def _default_ssh_runner(
    args: List[str], *, input: Optional[str] = None, timeout: Optional[float] = None
) -> Tuple[int, str, str]:
    """Default ``SshRunner``: invokes the real ``ssh`` CLI off the event-loop
    thread (never ``asyncio.create_subprocess_exec`` — same hazard the docker/
    local backends already dodge via ``run_in_executor``). ``args`` is the FULL
    argv INCLUDING the leading ``"ssh"`` token (built by ``_build_ssh_argv`` —
    unlike the docker runner there is no separate binary-name prefix to add).
    """
    stdin_bytes = input.encode() if input is not None else None

    def _run_sync():
        import subprocess
        try:
            proc = subprocess.Popen(
                args, env=build_child_env({}),
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,  # own process group -> killpg on timeout
            )
        except Exception as e:
            return 1, b"", f"ssh launch error: {type(e).__name__}: {e}".encode(), False
        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
            return proc.returncode, out, err, False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = b"", b""
            return (proc.returncode if proc.returncode is not None else 1), out, err, True

    loop = asyncio.get_event_loop()
    code, out, err, timed_out = await loop.run_in_executor(None, _run_sync)
    out_text = (out or b"").decode("utf-8", errors="replace")
    err_text = (err or b"").decode("utf-8", errors="replace")
    if timed_out:
        raise _SshRunnerTimeout(err_text or f"ssh (local CLI) timed out after {timeout}s")
    return code, out_text, err_text


class SshBackend(ExecutionBackend):
    """Runs ``run_code`` on a remote host over the system ``ssh`` binary.

    Zero-arg constructible (the registry factory contract) — every knob is read
    from the environment in the constructor, docker-backend precedent.
    """

    name = "ssh"

    def __init__(self, *, ssh_runner: Optional[SshRunner] = None) -> None:
        self.max_timeout = _float_env("CODE_EXEC_MAX_TIMEOUT_SEC", 30.0)
        self.max_output = _int_env("CODE_EXEC_MAX_OUTPUT_BYTES", 100000)
        self.host = os.getenv("CODE_EXEC_SSH_HOST", "")
        self.user = os.getenv("CODE_EXEC_SSH_USER", "")
        self.port = os.getenv("CODE_EXEC_SSH_PORT", "22")
        self.key = os.getenv("CODE_EXEC_SSH_KEY", "")
        self.sandboxed = _bool_env("CODE_EXEC_SSH_SANDBOXED", False)
        # Only the DEFAULT runner ever shells out to the real 'ssh' binary — an
        # injected runner (tests, or a future non-CLI transport) never does, so
        # setup()'s PATH check is scoped to exactly the path that needs it.
        self._using_default_runner = ssh_runner is None
        self._ssh_runner: SshRunner = ssh_runner or _default_ssh_runner

    # -- lifecycle --------------------------------------------------------

    async def setup(self) -> None:
        """Verify LOCAL prerequisites only: ``CODE_EXEC_SSH_HOST`` is set, and
        (default runner only) the ``ssh`` binary is on PATH. Deliberately NO live
        connection probe — a misconfigured/unreachable host is a ``run()``-time
        failure, reported honestly from the actual attempt rather than guessed at
        here.
        """
        if not self.host:
            raise ExecutionBackendError(
                "ssh backend selected but CODE_EXEC_SSH_HOST is not set. Set "
                "CODE_EXEC_SSH_HOST (and optionally CODE_EXEC_SSH_USER / "
                "CODE_EXEC_SSH_PORT / CODE_EXEC_SSH_KEY) to the target host."
            )
        if self._using_default_runner and shutil.which("ssh") is None:
            raise ExecutionBackendError(
                "ssh backend selected but the 'ssh' binary was not found on PATH."
            )

    async def teardown(self) -> None:  # ephemeral-only v1: nothing persists
        return None

    @property
    def capabilities(self) -> Dict[str, object]:
        return {"network": True, "isolation": "remote-host", "sandbox": self.sandboxed}

    # -- helpers ------------------------------------------------------------

    def _clamp_timeout(self, t) -> float:
        if t is None:
            return self.max_timeout
        return max(1.0, min(float(t), self.max_timeout))

    def _cap_text(self, text: Optional[str]):
        text = text or ""
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n...[truncated {len(text) - self.max_output} chars]", True
        return text, False

    def _build_env_prefix(self, env: Optional[Dict[str, str]]) -> str:
        """PURE: an ``env NAME=value ...`` prefix for the remote command line.

        The remote environment forwards NOTHING by default. An entry survives
        ONLY if it (a) passes the shared ``SECRET_PAT`` scrub (same policy every
        other backend uses) AND (b) has a syntactically valid POSIX identifier
        NAME (``_ENV_KEY_RE``) — anything else is silently dropped, never passed
        through raw. Every surviving VALUE is ``shlex.quote``d.
        """
        if not env:
            return ""
        parts = []
        for k, v in env.items():
            if SECRET_PAT.search(k):
                continue  # never let a caller smuggle a secret-named var in
            if not _ENV_KEY_RE.match(k):
                continue  # not a safe shell identifier — drop rather than risk syntax
            parts.append(f"{k}={shlex.quote(str(v))}")
        if not parts:
            return ""
        return "env " + " ".join(parts)

    def _build_remote_command(self, request: ExecutionRequest, timeout: float) -> str:
        """PURE: the single remote command STRING (see module docstring for the
        full quoting rationale) — ``[env K=V ...] timeout --signal=KILL <n>
        <interpreter> -c <shlex.quote(code)>``. Assumes ``request.language`` was
        already validated by the caller (``run()``).
        """
        lang = (request.language or "").lower()
        pieces: List[str] = []
        env_prefix = self._build_env_prefix(request.env)
        if env_prefix:
            pieces.append(env_prefix)
        pieces.append(f"timeout --signal=KILL {timeout}")
        if lang in _PY:
            pieces.append(f"python3 -c {shlex.quote(request.code)}")
        else:
            pieces.append(f"bash -c {shlex.quote(request.code)}")
        return " ".join(pieces)

    def _build_ssh_argv(self, request: ExecutionRequest) -> List[str]:
        """PURE (env-read only via ``self.*``, no subprocess — unit-testable
        hostless): the full ``ssh`` argv, ending in the wrapped remote command as
        a SINGLE trailing element.

        Shape: ``ssh -o BatchMode=yes -o ConnectTimeout=10 -o
        StrictHostKeyChecking=accept-new -p <port> [-i <key>] -- <user>@<host>
        <wrapped command>``. ``--`` comes BEFORE ``target`` — see the module
        docstring's "Option-injection safety" note for why the order matters.
        The key path is never logged anywhere — only ever placed into this
        argv, which callers must not log verbatim (``run()`` logs host/port/
        exit code only).

        Raises ``ExecutionBackendError`` (defense-in-depth, belt-and-suspenders
        alongside the ``--`` placement) if ``self.host`` or ``self.user``
        begins with ``-`` — such a value could otherwise be misread by OpenSSH
        as an option flag.
        """
        if self.host.startswith("-"):
            raise ExecutionBackendError(
                f"CODE_EXEC_SSH_HOST value {self.host!r} begins with '-' — "
                "refusing to build an ssh invocation OpenSSH could misinterpret "
                "as an option flag."
            )
        if self.user.startswith("-"):
            raise ExecutionBackendError(
                f"CODE_EXEC_SSH_USER value {self.user!r} begins with '-' — "
                "refusing to build an ssh invocation OpenSSH could misinterpret "
                "as an option flag."
            )
        timeout = self._clamp_timeout(request.timeout)
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(self.port),
        ]
        if self.key:
            argv += ["-i", self.key]
        target = f"{self.user}@{self.host}" if self.user else self.host
        argv += ["--", target, self._build_remote_command(request, timeout)]
        return argv

    # -- run ------------------------------------------------------------------

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        lang = (request.language or "").lower()
        if lang not in _PY and lang not in _SH:
            return ExecutionResult(
                stderr=f"unsupported language '{request.language}' (use python|bash)",
                exit_code=2, backend=self.name,
            )
        if not self.host:
            return ExecutionResult(
                stderr="CODE_EXEC_SSH_HOST is not set", exit_code=2, backend=self.name,
            )
        timeout = self._clamp_timeout(request.timeout)
        try:
            argv = self._build_ssh_argv(request)
        except ExecutionBackendError as e:
            # Belt-and-suspenders: _build_ssh_argv() already refuses a '-'-prefixed
            # host/user; surface it the same structured way as the other
            # validation failures above rather than letting it escape run()
            # uncaught (never touches the runner).
            return ExecutionResult(stderr=str(e), exit_code=2, backend=self.name)
        # Host-side backstop ONLY — deliberately looser than the in-command
        # `timeout --signal=KILL` (the real kill boundary, enforced on the remote
        # host itself); this just bounds a hung local `ssh` CLI client.
        host_backstop = timeout + 5

        start = time.monotonic()
        try:
            code, out, err = await self._ssh_runner(argv, input=request.stdin, timeout=host_backstop)
            timed_out = code in _TIMEOUT_EXIT_CODES
        except _SshRunnerTimeout as e:
            code, out, err, timed_out = 1, "", str(e), True

        # Log host + exit code ONLY — never the argv (it carries the key path).
        logger.debug(
            "ssh backend: host=%s port=%s exit=%s timed_out=%s",
            self.host, self.port, code, timed_out,
        )

        out_text, t1 = self._cap_text(out)
        err_text, t2 = self._cap_text(err)
        return ExecutionResult(
            stdout=out_text, stderr=err_text, exit_code=code, timed_out=timed_out,
            truncated=t1 or t2, duration_sec=time.monotonic() - start, backend=self.name,
        )
