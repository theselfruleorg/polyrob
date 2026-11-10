"""Host git over a tree that sandboxed code can write.

When model-written code runs in a sandbox (any code-execution backend other than
the local one), the workspace is the one thing the sandbox and the host share. A
program in the container can write ``.git/config`` and ``.git/hooks``; git runs
the commands those name (fsmonitor, hooks, filter and diff drivers, an ssh or
credential command) as the HOST identity on the next ``git status``. So in that
mode the host git child:

* gets ``-c`` overrides that switch off the vectors a ``-c`` can switch off
  (:data:`OVERRIDES`), and
* refuses a repository whose own config (scope ``local`` or ``worktree``) names
  any other command (:func:`repo_config_refusal`).

With the local backend the model already runs code as this identity, so the
repository config is trusted as before and nothing here applies.
"""
import os
import re
import subprocess
from typing import List, Optional

#: Every vector one ``-c`` closes whatever the repository says.
OVERRIDES: List[str] = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "protocol.ext.allow=never",
]

#: Keys the overrides already neutralise; a repository may carry them.
_OVERRIDDEN = {"core.fsmonitor", "core.hookspath", "diff.external", "protocol.ext.allow"}

#: Repository-config keys whose VALUE git runs as a command (names are lower-cased).
_COMMAND_KEY = re.compile(
    r"^(?:"
    r"core\.(?:sshcommand|pager|editor|askpass|gitproxy|alternaterefscommand)"
    r"|sequence\.editor"
    r"|interactive\.difffilter"
    r"|(?:filter|diff|merge)\..*\.(?:clean|smudge|process|textconv|command|driver)"
    r"|credential\.(?:.*\.)?helper"
    r"|gpg\.(?:.*\.)?program"
    r"|remote\..*\.(?:uploadpack|receivepack|vcs|proxy)"
    r"|submodule\..*\.update"
    r"|alias\..*"
    r"|pager\..*"
    r"|.*\.cmd"
    r")$",
    re.DOTALL,
)

_REPO_SCOPES = {"local", "worktree"}


def sandboxed_tree() -> bool:
    """True when model-written code runs in a sandbox, so the tree is not trusted.

    Reads ``CODE_EXEC_BACKEND`` with the default of ``tools.code_exec.get_backend_name``
    (importing that package here would grow the tools import cycle)."""
    name = os.getenv("CODE_EXEC_BACKEND", "local_subprocess")
    return (name or "").strip().lower() not in ("local", "local_subprocess")


def repo_config_refusal(workdir: str, env: dict, timeout: float = 10.0) -> Optional[str]:
    """Why host git must not run in ``workdir`` now, or None.

    Reads the config git would use (reading it runs nothing) and refuses when the
    repository's own scope names a command. No repository at all is not a refusal:
    the git call itself then says so. Any other read failure refuses."""
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "config", "--includes", "--show-scope",
             "--name-only", "--list", "-z"],
            cwd=workdir, env=env, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None  # no git: the real call reports it
    except Exception as e:
        return f"git refused: the repository config could not be read ({type(e).__name__})"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        if "not a git repository" in err or not os.path.exists(workdir):
            return None
        return ("git refused: the repository config could not be read "
                f"({err.strip()[:200] or f'git exited {proc.returncode}'})")
    parts = (proc.stdout or b"").decode("utf-8", "replace").split("\0")
    bad = sorted({key for scope, key in zip(parts[0::2], parts[1::2])
                  if scope in _REPO_SCOPES and key.lower() not in _OVERRIDDEN
                  and _COMMAND_KEY.match(key.lower())})
    if not bad:
        return None
    shown = ", ".join(bad[:5]) + (" …" if len(bad) > 5 else "")
    return ("git refused: this repository's own config names a command git would run on "
            f"the host ({shown}), and code in the sandbox can write that config. Remove "
            "the key from .git/config, or run git inside the sandbox.")
