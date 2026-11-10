"""110 W1b ratchet: ONE owner-tenant check — ``core.instance.is_owner_uid``.

"Is this tenant the owner" was re-derived at about a dozen sites, each with its own
reading of local mode (one read ``POLYROB_LOCAL`` directly, one hard-coded it), so two
gates could disagree about the same user id. Shipped code now asks ``is_owner_uid``
(or a wrapper that calls it). This test fails on a new copy:

* a direct call of ``is_owner_local_safe(`` outside ``core/instance.py``;
* a comparison of a user id with ``resolve_owner_principal()`` — directly, or through a
  name assigned from it in the same function.

``ALLOWED`` is shrink-only: a row may go, never come back.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("agents", "api", "cli", "core", "cron", "modules", "packs", "polyrob",
         "surfaces", "tools", "webview")
HOME = "core/instance.py"
#: path -> number of copies still allowed (shrink-only).
ALLOWED: dict = {}


def _is_principal_call(node) -> bool:
    """``resolve_owner_principal()`` with no argument: the owner a tenant is compared
    with. A call with arguments (``default_to_instance=False``) asks how the install is
    bound, which is not a tenant check."""
    return (isinstance(node, ast.Call) and not node.args and not node.keywords
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "resolve_owner_principal")


def _copies(tree) -> int:
    n = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _is_principal_call(node.value):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(
                    node.func, "id", getattr(node.func, "attr", "")) == "is_owner_local_safe":
                n += 1
            if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                sides = [node.left, *node.comparators]
                for side in sides:
                    inner = side.args[0] if (isinstance(side, ast.Call)
                                             and getattr(side.func, "id", "") == "str"
                                             and side.args) else side
                    if _is_principal_call(inner) or (
                            isinstance(inner, ast.Name) and inner.id in names):
                        n += 1
                        break
        if isinstance(fn, ast.Module):
            break  # module-level walk already covered every nested node
    return n


def _scan() -> dict:
    found = {}
    for root in ROOTS:
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel == HOME or "/tests/" in rel or "/_vendor/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            n = _copies(tree)
            if n:
                found[rel] = n
    return found


def test_no_new_owner_tenant_copy():
    found = _scan()
    new = {p: n for p, n in found.items() if n > ALLOWED.get(p, 0)}
    assert not new, ("an owner-tenant check re-derived outside core.instance.is_owner_uid "
                     f"— call is_owner_uid instead: {new}")


def test_the_allowed_rows_are_tight():
    found = _scan()
    loose = {p: n for p, n in ALLOWED.items() if found.get(p, 0) < n}
    assert not loose, f"lower or delete these ALLOWED rows: {loose}"


def test_the_ratchet_sees_a_copy():
    src = ("from core.instance import resolve_owner_principal\n"
           "def f(uid):\n    owner = resolve_owner_principal()\n    return uid == owner\n"
           "def g(uid):\n    return str(uid) == str(resolve_owner_principal())\n"
           "def h(uid):\n    return is_owner_local_safe(uid, owner_principal=None,"
           " local_enabled=False)\n")
    assert _copies(ast.parse(src)) == 3
