"""Shared child-process env policy for code_exec backends (P0-B).

Extracted from ``local_subprocess.py`` so EVERY execution backend (local, docker,
future e2b/modal) builds its child environment through ONE secret-scrub, preventing
drift as backends multiply. The rule: inherit only an explicit host allowlist, never
inherit or accept a secret-NAMED variable (``*_API_KEY``/``*_TOKEN``/…), and drop
anything secret-looking even if it was allowlisted.

Holds NO ``@BaseTool.action`` closures, so ``from __future__ import annotations`` is safe.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, Optional

# Only these host env vars are ever passed through to a child. Secrets never are.
SAFE_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "TERM",
    "PWD", "SHELL", "USER", "PYTHONHASHSEED",
}

# Any env var whose NAME matches this is treated as a secret and never forwarded.
SECRET_PAT = re.compile(
    r"(API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY|MNEMONIC|SEED|CREDENTIAL)",
    re.IGNORECASE,
)


def build_child_env(
    extra: Optional[Dict[str, str]] = None,
    *,
    extra_allowlist: Optional[Iterable[str]] = None,
    allow_prefixes: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Return a scrubbed child environment.

    1. Inherit ONLY the ``SAFE_ALLOWLIST`` host vars that are actually set,
       widened by ``extra_allowlist`` (exact names) and ``allow_prefixes``
       (name prefixes, e.g. ``PLAYWRIGHT_``).
    2. Overlay ``extra`` (caller-supplied), skipping any secret-NAMED key so a caller
       cannot smuggle a secret in.
    3. Defensively drop anything whose name looks secret even if allowlisted.

    With both widening arguments omitted this is byte-identical to the original
    ``LocalSubprocessBackend._build_env``.

    ``extra_allowlist``/``allow_prefixes`` exist because a child that is OUR OWN
    binary (Chromium, the Playwright CLI, ``git``, the ``anysite`` CLI) genuinely
    needs host vars a sandboxed *agent-authored* script must never see —
    ``DISPLAY``, ``XAUTHORITY``, ``PLAYWRIGHT_BROWSERS_PATH``, proxy settings.
    Widening the INHERIT set is not widening the SECRET set: step 3 still runs
    over everything, so ``AGENT_WALLET_MASTER_SEED`` and every ``*_API_KEY``
    are dropped no matter which list named them.
    """
    names = set(SAFE_ALLOWLIST)
    names.update(extra_allowlist or ())
    prefixes = tuple(allow_prefixes or ())
    env = {k: v for k, v in os.environ.items()
           if k in names or (prefixes and k.startswith(prefixes))}
    for k, v in (extra or {}).items():
        if SECRET_PAT.search(k):
            continue  # never let a caller smuggle a secret-named var in
        env[k] = str(v)
    return {k: v for k, v in env.items() if not SECRET_PAT.search(k)}
