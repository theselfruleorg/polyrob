"""SEC-1 backstop: does a directory look like a secrets/code tree?

Used to WARN (or optionally refuse) when the agent's persistent workspace is the
cwd default AND that cwd contains secrets/VCS — the launch-in-a-repo footgun
(memory rob-glm-livetest-paramodel-bug-2026-06-20). This is a STEER, not a
read-denial guard; real per-read denial belongs at the filesystem/coding
chokepoint in a separate hardening PR (analysis §8.3).
"""
import os
from typing import List

_EXACT = {".env", ".env.production", ".env.development", ".git",
          "id_rsa", "credentials.json"}
_SUFFIX = (".pem",)


def looks_like_secrets_tree(path: str) -> List[str]:
    found: List[str] = []
    try:
        for name in os.listdir(path):
            if name in _EXACT or name.endswith(_SUFFIX):
                found.append(name)
    except OSError:
        return []
    return sorted(found)
