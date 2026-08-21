"""Private-teardown ratchet (2026-08-21 X/Twitter outage).

``_cleanup()`` is the private half of a two-part lifecycle contract: the
public ``cleanup()`` takes the lock, delegates to ``_cleanup()``, then clears
``_initialized``. Calling the private half on ANOTHER object performs the
destruction while skipping the bookkeeping — the object keeps claiming
``is_initialized`` over destroyed state, so the
``load_tools_from_container`` re-init gate never fires and a process-wide
singleton stays dead until restart. That was the whole outage.

This ratchet forbids any production call of ``<receiver>._cleanup(...)``
where the receiver is not ``self`` (or ``cls``/``super()``). The allowlist is
EMPTY and must stay empty — teardown from outside a class goes through the
public ``cleanup()``.

See the 2026-08-21 singleton-teardown incident.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROD_DIRS = ("agents", "tools", "core", "api", "cli", "modules", "surfaces", "cron", "webview")

# (repo-relative-path, lineno) — may only SHRINK. Currently empty; keep it so.
ALLOWLIST = frozenset()


def _receiver_is_self(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return True  # bare _cleanup() inside the class — fine
    value = func.value
    if isinstance(value, ast.Name) and value.id in ("self", "cls"):
        return True
    # super()._cleanup(...)
    if (isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "super"):
        return True
    return False


def _scan():
    offenders = []
    for top in PROD_DIRS:
        root = REPO / top
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_cleanup"
                        and not _receiver_is_self(node)):
                    rel = str(path.relative_to(REPO))
                    if (rel, node.lineno) not in ALLOWLIST:
                        offenders.append(f"{rel}:{node.lineno}")
    return offenders


def test_no_cross_object_private_cleanup_calls():
    offenders = _scan()
    assert not offenders, (
        "Cross-object _cleanup() call(s) found — use the public cleanup() so "
        "_initialized stays honest (2026-08-21 outage class):\n  "
        + "\n  ".join(offenders)
    )
