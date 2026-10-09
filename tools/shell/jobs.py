"""Background-job plumbing shared by both shell executors (073 W4).

ONE launcher text for the container and the host, so a job looks the same to the
`process` tool wherever it runs:

- ``<id>.pid``  — the launcher's pid (it heads the job's process group);
- ``<id>.log``  — the job's stdout+stderr;
- ``<id>.rc``   — the job's exit code, written by the launcher when the job ends
  (absent after a kill: the launcher dies with the group);
- ``<id>.in``   — a FIFO that IS the job's stdin (``process`` write/submit/close);
- ``<id>.keeper`` — the pid of the stdin KEEPER, a ``sleep`` that holds the FIFO's
  write side open;
- ``<id>.closed`` — present once ``process close`` sent EOF;
- ``<id>.jpid`` — the job shell's own pid (a kill reaches its children even
  where the launcher is not a process-group leader).

Why a keeper process (the documented choice, not ``O_RDWR``): opening a FIFO for
reading blocks until a writer opens it, and a reader sees EOF as soon as the LAST
writer closes. With a keeper:
  * the job's ``< fifo`` open rendezvouses with the keeper's ``> fifo`` open at
    launch — the job never blocks on the open;
  * a ``process write`` is a short-lived extra writer; when it closes, the keeper
    still holds the write side, so the job does NOT see EOF between writes;
  * ``process close`` kills the keeper -> no writer is left -> the job reads EOF.
  ``O_RDWR`` in the launcher would also avoid the open block, but the job would
  then hold a writer to its own stdin and could never see EOF. The keeper is in
  the job's process group, so a group kill reaps it; the launcher kills it when
  the job exits. Where ``mkfifo`` is missing the job gets ``/dev/null`` (no
  stdin; ``write`` says so).

No ``@BaseTool.action`` closures here — ``from __future__`` is safe.
"""
from __future__ import annotations

import re
import shlex
from typing import Dict

#: Max bytes one ``process write``/``submit`` sends.
STDIN_MAX_BYTES = 64 * 1024
#: Seconds a stdin write may wait for the job to take the bytes.
STDIN_WRITE_TIMEOUT_SEC = 5.0
#: A keeper sleeps "forever" (68 years); it is killed when the job ends.
_KEEPER_SLEEP = "2147483647"

#: A log whose LAST line looks like this is a job waiting for a secret. The agent
#: never types a password into a job (cross-agent parity: the guard against an
#: agent-supplied password on stdin).
_PASSWORD_PROMPT_RE = re.compile(
    r"(?i)(\[sudo\]|password|passphrase|passcode|\bpin\b|verification code|"
    r"one-time code|\botp\b|secret|token)[^\n]{0,80}[:?>]\s*$"
)


def ends_with_password_prompt(log_tail: str) -> bool:
    """True when the job's output currently ends at a password-style prompt."""
    from tools.shell.output import strip_ansi
    text = strip_ansi(log_tail or "").replace("\r", "\n").rstrip(" \t")
    if not text:
        return False
    last = text.rsplit("\n", 1)[-1]
    if not last.strip():
        return False
    return bool(_PASSWORD_PROMPT_RE.search(last[-200:]))


def job_files(jobs_dir: str, job_id: str) -> Dict[str, str]:
    """The control-file paths for ``job_id`` (``job_id`` must already be validated)."""
    base = f"{jobs_dir.rstrip('/')}/{job_id}"
    return {k: f"{base}.{k}" for k in ("pid", "log", "cmd", "rc", "in", "keeper", "closed", "jpid")}


def launcher_script(*, shell: str, files: Dict[str, str], exports: str,
                    pre: str = "") -> str:
    """The launcher every background job runs under (container and host).

    ``shell`` (already shell-safe text, e.g. ``sh`` or a quoted bash path plus
    flags) runs the ``.cmd`` file verbatim (zero quoting hazard); ``exports``
    replays the persisted user env; ``pre`` runs first (``mkdir`` of the jobs dir
    in the container). The launcher waits for the job, then writes the exit code
    and stops the stdin keeper.
    """
    q = shlex.quote
    f = {k: q(v) for k, v in files.items()}
    sh = shell
    return (
        f"{pre}"
        f"{exports}"
        f"echo $$ > {f['pid']}\n"
        f"rm -f {f['in']} {f['keeper']} {f['closed']} {f['rc']}\n"
        f"if mkfifo -m 600 {f['in']} 2>/dev/null; then\n"
        f"  sleep {_KEEPER_SLEEP} > {f['in']} 2>/dev/null &\n"
        f"  echo $! > {f['keeper']}\n"
        f"  {sh} {f['cmd']} < {f['in']} > {f['log']} 2>&1 &\n"
        f"else\n"
        f"  {sh} {f['cmd']} < /dev/null > {f['log']} 2>&1 &\n"
        f"fi\n"
        f"__polyrob_job=$!\n"
        f"echo $__polyrob_job > {f['jpid']}\n"
        f"wait $__polyrob_job\n"
        f"__polyrob_rc=$?\n"
        f"if [ -f {f['keeper']} ]; then kill \"$(cat {f['keeper']})\" 2>/dev/null; fi\n"
        f"echo $__polyrob_rc > {f['rc']}\n"
    )


def command_file_text(command: str, workdir: str = "") -> str:
    """The ``.cmd`` text: an optional one-job directory, then the command verbatim."""
    if workdir:
        return f"cd {shlex.quote(workdir)} || exit 2\n{command}"
    return command


__all__ = [
    "STDIN_MAX_BYTES", "STDIN_WRITE_TIMEOUT_SEC", "ends_with_password_prompt",
    "job_files", "launcher_script", "command_file_text",
]
