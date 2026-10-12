"""W11 — every third-party import in the shipped packages is DECLARED.

The httpx incident class: a module imports a distribution that only some OTHER
dependency happened to pull in (bs4 via markdownify, yarl via aiohttp,
eth_utils via web3). The day that other dependency drops it, the import breaks
on an install nobody tested. This test AST-scans every import (module level AND
function level) in the packages the wheel ships and asserts each top-level name
is the standard library, a shipped package, a dependency or extra declared in
``pyproject.toml``, or a row of ``OPTIONAL_UNDECLARED`` (a guarded import with a
fallback — the only allowed exception, and the list may only shrink).

⚠️ Ships publicly: reads ``pyproject.toml`` and the shipped trees only.
"""
import ast
import re
import sys
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

#: Import name -> distribution, where the two differ: the ONE mapping.
from core.optional_extras import IMPORT_TO_DIST  # noqa: E402

#: Imported, NOT declared, on purpose: each is guarded (``try/except
#: ImportError``) and the code works without it. Shrink-only — a new row needs a
#: reason; a row whose import is gone fails the stale check below.
OPTIONAL_UNDECLARED = {
    "cachetools": "agents/task/path.py: the session LRU cache; a manual LRU fallback",
    "filelock": "core/file_lock.py: SafeFileLock; a plain lock-file fallback",
    "nest_asyncio": "modules/llm/adapters.py: the sync adapter's nested-loop patch; guarded",
    "posthog": "agents/task/telemetry/service.py: optional product telemetry; off without it",
    "pysqlite3": "api/app.py: a newer sqlite3 build when present; stdlib sqlite3 otherwise",
    "tomli": "core/version.py: TOML on Python < 3.11; requires-python is >= 3.11",
    "polyrob_drop": "tools/agent_nft/tool.py: the separate NFT-drop add-on, probed by name",
    "polyrob_desk": "tools/agent_nft/tool.py: the separate Desk add-on, probed by name",
}

_SKIP_PARTS = {"tests", "node_modules", "__pycache__", "dev"}


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _project() -> dict:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _declared() -> set:
    project = _project()["project"]
    specs = list(project["dependencies"])
    for name, extra in project.get("optional-dependencies", {}).items():
        specs += [s for s in extra if not s.startswith("polyrob[")]
    return {_norm(_NAME_RE.match(s.strip()).group(0)) for s in specs}


def _shipped_roots() -> list:
    """(directory, top-level package name) for every package the wheel ships."""
    find = _project()["tool"]["setuptools"]["packages"]["find"]
    include = [p.rstrip("*") for p in find["include"]]
    roots = []
    for where in find["where"]:
        base = _REPO / where
        for name in include:
            if (base / name / "__init__.py").is_file() or (base / name).is_dir() and name in (
                    "data", "assets"):
                roots.append((base / name, name))
    return roots


def _imports() -> dict:
    """top-level import name -> sorted files (repo-relative) importing it."""
    seen: dict = {}
    for root, _ in _shipped_roots():
        for path in root.rglob("*.py"):
            rel = path.relative_to(_REPO)
            if _SKIP_PARTS & set(rel.parts[:-1]):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    seen.setdefault(name.split(".")[0], set()).add(str(rel))
    return {k: sorted(v) for k, v in seen.items()}


def _undeclared() -> dict:
    internal = {name for _, name in _shipped_roots()} | {"packs"}
    declared = _declared()
    out = {}
    for name, files in _imports().items():
        if name in sys.stdlib_module_names or name in internal or name.startswith("_"):
            continue
        if _norm(IMPORT_TO_DIST.get(name, name)) in declared:
            continue
        out[name] = files
    return out


def test_every_third_party_import_is_declared():
    missing = {k: v for k, v in _undeclared().items() if k not in OPTIONAL_UNDECLARED}
    assert not missing, (
        "imported but not declared in pyproject.toml (base or an extra): "
        + "; ".join(f"{k} ({v[0]})" for k, v in sorted(missing.items()))
        + ". Declare the distribution in the extra that owns it (relock with "
        "`python devtools/gen_lazy_closures.py --relock`), map it in IMPORT_TO_DIST "
        "when the import name differs, or — only for a guarded import with a "
        "fallback — add an OPTIONAL_UNDECLARED row with the reason."
    )


def test_optional_undeclared_rows_are_live_and_really_undeclared():
    undeclared = _undeclared()
    stale = sorted(set(OPTIONAL_UNDECLARED) - set(undeclared))
    assert not stale, f"OPTIONAL_UNDECLARED rows no longer imported or now declared: {stale}"


def test_the_scan_sees_the_shipped_trees():
    names = {name for _, name in _shipped_roots()}
    assert {"core", "tools", "agents", "cli", "polyrob_x"} <= names
    assert "aiohttp" in _imports() and "eth_utils" in _imports()
