"""Child-process environment for the CLI's `git clone` paths (S2 / M4).

``cli/commands/profile_dist.py`` and ``cli/commands/skill_install.py`` both clone
an ATTACKER-NAMED repository ("install my profile: <url>", "install skill
owner/repo"). Both built the child environment from the whole process
environment (``dict(os.environ)``), so ``git`` — and every hook, filter and
credential helper it can be talked into running — saw
``AGENT_WALLET_MASTER_SEED`` and every provider API key.

This is the ONE git-child env for those two call sites: the shared scrub
(:func:`tools.code_exec.env_policy.build_child_env`) widened by exactly what git
needs to reach a remote, plus the hardening flags both sites already set.
``tools/git/tool.py`` builds an equivalent (even narrower) env inline for the
agent-facing git tool.

Lives in ``cli/`` rather than ``cli/commands/`` on purpose: it registers no
click command, and every module under ``cli/commands/`` must load the env-file
ladder (``tests/unit/cli/test_cli_env_loading_ratchet.py``).
"""
from __future__ import annotations

from typing import Dict

#: Host vars git needs on top of the base allowlist. None can hold a POLYROB
#: credential; ``SSH_AUTH_SOCK`` is a handle to the operator's own agent, which
#: an ssh:// clone of a private repo cannot work without.
GIT_ENV_ALLOWLIST = (
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    # Windows
    "SystemRoot", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "PROGRAMFILES", "PROGRAMDATA", "COMSPEC", "PATHEXT", "TEMP", "TMP",
)


def build_git_child_env(**extra: str) -> Dict[str, str]:
    """Scrubbed git environment with the hardening flags both clone paths set.

    Defaults applied (a caller's ``extra`` wins):

    * ``GIT_TERMINAL_PROMPT=0`` — never block on a credential prompt.
    * ``GIT_CONFIG_NOSYSTEM=1`` — ignore ``/etc/gitconfig``.
    * ``GIT_ALLOW_PROTOCOL`` — real transports only. git's default set includes
      ``ext::``/``fd::``, which run an arbitrary command at clone time (= RCE on
      an attacker-supplied source string).
    """
    from tools.code_exec.env_policy import build_child_env
    env = build_child_env(extra_allowlist=GIT_ENV_ALLOWLIST)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    env.setdefault("GIT_ALLOW_PROTOCOL", "file:git:http:https:ssh")
    env.update({k: str(v) for k, v in extra.items()})
    return env
