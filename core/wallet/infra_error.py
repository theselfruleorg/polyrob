"""Classify a money-verb failure as HOST/INFRA (nothing can succeed this run).

2026-09-16 23:35Z → 09-17 05:52Z every broadcast failed
``broadcast failed: [Errno 13] Permission denied: '/var/lib/polyrob/wallet/
submissions.sqlite' — nothing was sent`` (the identity-hardening regression).
The EXIT rails hit it, then spent their step budgets on host forensics
(``shell_run ls -la``, ``run_code``) until "Task not completed after 8 steps",
and the owner heard about it 5 h later from a different rail with the wrong
cause. A broadcast that fails for a host reason is an ops fact: the run should
END with the verbatim line, not investigate.

Deliberately narrow: only the money rails' own ``broadcast failed:`` prefix
(tools/defi/trade_tool.py, tools/launchpad/execute.py, tools/defi/bridge_verb.py)
combined with an OS-level token. A guard refusal, a revert, a route miss or a
cap refusal is NOT infra and must keep flowing to the model.
"""
import re

_BROADCAST = re.compile(r"broadcast failed:", re.I)
_INFRA = re.compile(
    r"\[Errno \d+\]|Permission denied|Read-only file system|OSError|"
    r"No such file or directory|disk I/O error|database is locked|unable to open database",
    re.I,
)


def is_infra_broadcast_failure(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return bool(_BROADCAST.search(text) and _INFRA.search(text))
