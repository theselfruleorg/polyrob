"""The ``agents/task/constants.py`` back-compat shim may only lose importers (W7).

WS-1 (2026-07-16) moved the autonomy/posture/payment-policy cluster to
``core.config_policy``; ``agents/task/constants.py`` re-exports it so old imports
keep working. New code imports from ``core.config_policy``. This ratchet counts
the places that still fetch a ``core.config_policy`` name THROUGH the shim:

* ``from agents.task.constants import <config_policy name>`` statements, and
* ``"agents.task.constants.<config_policy name>"`` dotted strings (patch targets).

The count may only shrink. When it reaches zero, delete the re-export block in
``agents/task/constants.py`` and this test.
"""
import ast
import re
import subprocess

from tests.support.repo_index import REPO_ROOT, repo_index

SHIM = "agents/task/constants.py"

# Remaining: the goal board and dispatcher (W9 splits in flight on 2026-10-10).
CEILING = 16

_DOTTED = re.compile(r"agents\.task\.constants\.([A-Za-z_]\w*)")


def _config_policy_names():
    import core.config_policy as cp
    from core.config_policy import payment_tools

    native = set()
    for node in repo_index().tree(REPO_ROOT / SHIM).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            native.add(node.name)
        elif isinstance(node, ast.Assign):
            native |= {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            native.add(node.target.id)
    names = {n for n in dir(cp) if not n.startswith("__")} | set(payment_tools.LAZY_VIEWS)
    return names - native


def _shim_uses():
    names = _config_policy_names()
    out = subprocess.run(["git", "grep", "-l", "agents.task.constants", "--", "*.py"],
                         cwd=REPO_ROOT, capture_output=True, text=True, timeout=60).stdout.split()
    uses = []
    for rel in out:
        if rel == SHIM:
            continue
        for node in repo_index().nodes(REPO_ROOT / rel):
            if (isinstance(node, ast.ImportFrom) and node.level == 0
                    and node.module == "agents.task.constants"
                    and any(a.name in names for a in node.names)):
                uses.append(f"{rel}:{node.lineno}")
        for m in _DOTTED.finditer(repo_index().text(REPO_ROOT / rel)):
            if m.group(1) in names:
                uses.append(f"{rel}: {m.group(0)}")
    return sorted(uses)


def test_shim_importers_only_shrink():
    uses = _shim_uses()
    assert len(uses) <= CEILING, (
        f"{len(uses)} place(s) fetch a core.config_policy name through the "
        f"agents/task/constants.py shim (ceiling {CEILING}). Import it from "
        "core.config_policy instead:\n" + "\n".join(uses)
    )
    assert len(uses) == CEILING, (
        f"The shim lost importers ({len(uses)} < {CEILING}): lower CEILING to {len(uses)}."
    )
