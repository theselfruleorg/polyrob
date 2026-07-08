"""Pure snapshot-replay state for the persistent shell (WS-2).

The shell is stateful across `shell_run` calls, but NOT via a single long-lived
interactive process (which deadlocks on partial reads). Instead — Hermes' model —
each command is wrapped so it cd's into the saved cwd, re-exports the saved user
env, runs, then emits sentinel-framed `pwd`/`env`. The tool parses that trailing
block to persist cwd + user env for the next call and strips it from the output
the model sees.

No docker/subprocess here — this module is pure so the state machine is fully
unit-testable. The executor (`tools/shell/executor.py`) runs `wrap_command`'s text
in the sandbox and feeds the raw stdout back into `parse_state`.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Dict, Tuple

# A record-separator-framed sentinel that ordinary command output is astronomically
# unlikely to emit. \x1e (RS) is a control char no normal build/test output prints.
STATE_SENTINEL = "\x1e__POLYROB_SHELL_STATE__\x1e"
_CWD_MARK = "\x1e__CWD__\x1e"
_ENV_MARK = "\x1e__ENV__\x1e"

#: Env keys the shell/container manages itself — never carried forward as "user"
#: env (they'd fight the wrapper's own cd/export and the WS-1 dev defaults).
_SHELL_MANAGED_ENV = frozenset({
    "PWD", "OLDPWD", "SHLVL", "_", "HOME", "HOSTNAME", "PATH", "TERM",
    "PYTHONPATH", "PIP_TARGET", "LANG", "LC_ALL",
})

#: Env NAMES that must NEVER be persisted+replayed as an `export` on the next call —
#: they cause code to load/run out of band (LD_PRELOAD etc), so a hostile value that
#: reaches the env trailer (via a forged block OR a newline-in-value phantom var, since
#: `env` output is line-split) would otherwise poison every subsequent command in the
#: session. Dropped regardless of value.
_DANGEROUS_ENV_NAMES = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV", "IFS", "PROMPT_COMMAND", "PS4",
    "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "GIT_SSH_COMMAND",
    "GIT_EXTERNAL_DIFF", "PERL5OPT", "PERL5LIB", "RUBYOPT", "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
})
#: Env NAME prefixes with the same hazard (exported bash functions; any LD_*/DYLD_*).
_DANGEROUS_ENV_PREFIXES = ("BASH_FUNC_", "LD_", "DYLD_")

_MAX_ENV_VALUE_CHARS = 4096  # don't persist a pathological multi-KB env value


@dataclass
class ShellState:
    """Persisted per-session shell state: cwd + user-set env vars."""

    cwd: str = "/workspace"
    env: Dict[str, str] = field(default_factory=dict)


def wrap_command(command: str, state: ShellState) -> str:
    """Build the sandbox script that runs ``command`` with persisted cwd+env and
    emits the trailing state block. The command's own exit status is preserved.

    Everything derived from persisted state (cwd, env values) is single-quoted via
    ``shlex.quote`` so a hostile saved value can never break out of the wrapper —
    only the model-supplied ``command`` runs unquoted (that IS the shell surface).
    """
    lines = [f"cd {shlex.quote(state.cwd)} 2>/dev/null || cd /workspace"]
    for k, v in state.env.items():
        # keys are validated on parse (identifier-shaped); values are quoted.
        lines.append(f"export {k}={shlex.quote(v)}")
    lines.append(command)
    lines.append("__polyrob_rc=$?")
    # Emit the state block. printf (not echo) so the sentinels are literal.
    lines.append(f"printf '%s' {shlex.quote(STATE_SENTINEL)}")
    lines.append(f"printf '%s' {shlex.quote(_CWD_MARK)}; pwd")
    lines.append(f"printf '%s' {shlex.quote(_ENV_MARK)}; env")
    lines.append("exit $__polyrob_rc")
    return "\n".join(lines)


def _is_env_key(k: str) -> bool:
    return bool(k) and (k[0].isalpha() or k[0] == "_") and all(
        c.isalnum() or c == "_" for c in k
    )


def parse_state(raw_stdout: str, prev: ShellState) -> Tuple[str, ShellState]:
    """Split raw stdout into (clean output, new ShellState).

    If the sentinel is absent (the command was killed before the trailer ran, or
    output was truncated), the previous state is preserved unchanged and the full
    output is returned — a missing trailer must never reset cwd/env.
    """
    # rfind (LAST occurrence), NOT find: the genuine trailer is always appended LAST
    # by wrap_command, so a command whose OWN stdout emits a forged sentinel earlier
    # (or cats untrusted content containing it) cannot hijack cwd/env — the real
    # trailing block wins.
    idx = raw_stdout.rfind(STATE_SENTINEL)
    if idx < 0:
        return raw_stdout.rstrip("\n"), ShellState(cwd=prev.cwd, env=dict(prev.env))

    clean = raw_stdout[:idx].rstrip("\n")
    block = raw_stdout[idx + len(STATE_SENTINEL):]

    cwd = prev.cwd
    env: Dict[str, str] = {}

    cwd_at = block.find(_CWD_MARK)
    env_at = block.find(_ENV_MARK)
    if cwd_at >= 0:
        end = env_at if env_at > cwd_at else len(block)
        cwd_val = block[cwd_at + len(_CWD_MARK):end].strip()
        if cwd_val:
            cwd = cwd_val
    if env_at >= 0:
        env_text = block[env_at + len(_ENV_MARK):]
        for line in env_text.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if not _is_env_key(k) or k in _SHELL_MANAGED_ENV:
                continue
            if k in _DANGEROUS_ENV_NAMES or k.startswith(_DANGEROUS_ENV_PREFIXES):
                continue  # never replay LD_PRELOAD/BASH_ENV/BASH_FUNC_*/... as an export
            if len(v) > _MAX_ENV_VALUE_CHARS:
                continue
            env[k] = v

    return clean, ShellState(cwd=cwd, env=env)
