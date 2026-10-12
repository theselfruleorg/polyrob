"""Which shipped modules cannot import on a BARE install (W11).

Derived, not hand-kept: a module needs an extra when one of its UNGUARDED
module-level imports (not inside ``try/except ImportError``, not under
``TYPE_CHECKING``) is a module ``core.optional_extras.EXTRA_FOR_MODULE`` maps to
an extra — directly, or through another module of the same tree that does, or
through its parent package. A module that needs an extra for a reason the AST
cannot see (an import-time CALL into a lazily-importing helper) says so with a
module-level ``__requires_extra__ = "<extra>"``. The CI base-install job walks ``core`` bare and
asserts the set of ImportErrors is EXACTLY this set (ci.yml "every core module
imports with no extras"); it replaced a hand list (NEEDS_EXTRA).

Stdlib + core only: the base-install job runs it with no extra installed.
"""
import ast
from pathlib import Path
from typing import Dict, List, Optional, Set

_GUARD_EXC = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _guards_import(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in _GUARD_EXC for n in names)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def module_level_imports(path: Path, modname: str) -> List[str]:
    """Absolute dotted names a module imports unguarded at module level
    (``from a import b`` yields both ``a`` and ``a.b``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__requires_extra__"
                and isinstance(node.value, ast.Constant)):
            return [f"__requires_extra__:{node.value.value}"]
    is_pkg = path.name == "__init__.py"
    out: List[str] = []

    def visit(body, guarded: bool) -> None:
        for node in body:
            if isinstance(node, ast.Try):
                visit(node.body, guarded or any(_guards_import(h) for h in node.handlers))
                visit(node.orelse, guarded)
                visit(node.finalbody, guarded)
            elif isinstance(node, ast.If):
                if not _is_type_checking(node.test):
                    visit(node.body, guarded)
                    visit(node.orelse, guarded)
            elif guarded:
                continue
            elif isinstance(node, ast.Import):
                out.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pkg = modname.split(".") if is_pkg else modname.split(".")[:-1]
                    pkg = pkg[:len(pkg) - (node.level - 1)]
                    base = ".".join(pkg + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                out.append(base)
                out.extend(f"{base}.{a.name}" for a in node.names)

    visit(tree.body, False)
    return out


def modules_needing_extras(root: Path, package: str = "core") -> Dict[str, str]:
    """``module -> the extra it needs`` for every module of *package* under *root*
    that cannot import without an extra."""
    from core.optional_extras import EXTRA_FOR_MODULE
    mods: Dict[str, Path] = {}
    for path in (Path(root) / package).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods[".".join(parts)] = path
    imports = {m: module_level_imports(p, m) for m, p in mods.items()}
    memo: Dict[str, Optional[str]] = {}

    def need(mod: str, stack: Set[str]) -> Optional[str]:
        if mod in memo:
            return memo[mod]
        stack = stack | {mod}
        parents = [".".join(mod.split(".")[:i]) for i in range(1, mod.count(".") + 1)]
        found = None
        for imp in imports.get(mod, []) + parents:
            if imp.startswith("__requires_extra__:"):
                found = imp.split(":", 1)[1]
                break
            top = imp.split(".")[0]
            if top in EXTRA_FOR_MODULE:
                found = EXTRA_FOR_MODULE[top]
                break
            if imp in mods and imp not in stack:
                found = need(imp, stack)
                if found:
                    break
        if not stack - {mod}:  # only memoise from a root call (cycles stay exact)
            memo[mod] = found
        return found

    return {m: e for m in sorted(mods) if (e := need(m, set()))}
