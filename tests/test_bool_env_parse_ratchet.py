"""Open-coded boolean truth sets may only SHRINK.

``os.getenv(...).lower() in ("1", "true", "yes", "on")`` is an opt-in
ALLOW-list; the repo's one flag parser (``core.env.parse_bool``/``bool_env``)
is a falsey DENY-list (only ``none/off/false/0/no/''`` disable). The two
disagree on every other spelling — ``enabled``, ``yes please``, ``TRUE ``
— which is how a flag reads ON in one module and OFF in the one next to it
(the reflection-gate bug was exactly a parser/source mismatch). Sites that
parse a QUERY or MCP parameter rather than an env flag are legitimately
allow-lists and stay; the baseline counts them. Lower it as env sites
migrate, never raise it.
"""
import pathlib
import re

from tests.support.repo_index import repo_index

_BASELINE = 3   # 2026-10-10: core/prefs._coerce (a validated pref VALUE that rejects
                # unknown spellings), tools/mcp/param_coercion (MCP param coercion) and
                # tools/defi/providers/goplus (a provider's own "1"/"true"/"True" wire
                # encoding) — none of them reads an env flag.
_SKIP = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "deployment", "docs", "scripts", "build", "dist"}
_PAT = re.compile(
    r"""in\s*[\(\{]\s*(?:"|')(?:1|true|yes|on|True)(?:"|')\s*,\s*(?:"|')(?:1|true|yes|on|True)(?:"|')""")


def _count():
    repo = pathlib.Path(__file__).resolve().parents[1]
    count = 0
    offenders = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if _SKIP.intersection(rel.parts):
            continue
        try:
            hits = len(_PAT.findall(repo_index().text(p, errors="ignore")))
        except OSError:
            continue
        if hits:
            count += hits
            offenders[str(rel)] = hits
    return count, offenders


def test_open_coded_truth_sets_never_grow():
    count, offenders = _count()
    assert count <= _BASELINE, (
        f"open-coded boolean truth sets grew to {count} (baseline {_BASELINE}). "
        f"Parse env flags with core.env.bool_env/parse_bool/bool_from. "
        f"Offenders: {offenders}")


def test_baseline_tracks_actual():
    """Tightness: a migration that forgets to lower _BASELINE stops the ratchet."""
    count, offenders = _count()
    assert count >= _BASELINE, (
        f"open-coded truth sets fell to {count}; lower _BASELINE ({_BASELINE}) "
        f"to {count}. Remaining: {offenders}")


# --- strict comparisons: ``os.getenv("X", "false").lower() == "true"`` -------
#
# The other open-coded shape. It is an allow-list of ONE spelling: "1", "yes"
# and "on" read as OFF, so the same value turns a flag on in one module and
# off in the next. Found by AST (the comparison spans lines), over the shipped
# source dirs. Zero tolerance since 2026-10-10 (the 19 sites moved to
# core.env.bool_env / parse_bool).
import ast  # noqa: E402
import subprocess  # noqa: E402

_SOURCE_DIRS = ("core", "polyrob", "modules", "tools", "agents", "cli", "webview",
                "surfaces", "cron", "api", "utils", "packs", "main.py")
_TRUTH_WORDS = {"true", "1", "yes", "on", "false", "0", "no", "off"}


def _is_env_read(node) -> bool:
    while (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr in ("lower", "strip", "upper", "casefold")):
        node = node.func.value
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("getenv", "get"):
            base = ast.unparse(func.value)
            if func.attr == "getenv" or base.endswith("environ") or base == "env":
                return True
        if isinstance(func, ast.Name) and func.id == "str" and node.args:
            return _is_env_read(node.args[0])
    if isinstance(node, ast.BoolOp):
        return any(_is_env_read(v) for v in node.values)
    if isinstance(node, ast.Subscript):
        return ast.unparse(node.value).endswith("environ")
    return False


def _strict_sites():
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = subprocess.run(["git", "ls-files", "--", *_SOURCE_DIRS], cwd=repo,
                         capture_output=True, text=True, timeout=30).stdout
    sites = []
    for rel in out.splitlines():
        path = repo / rel
        if not rel.endswith(".py") or "/tests/" in rel or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
                rhs = node.comparators[0]
                if (isinstance(rhs, ast.Constant) and isinstance(rhs.value, str)
                        and rhs.value.lower() in _TRUTH_WORDS
                        and _is_env_read(node.left)):
                    sites.append(f"{rel}:{node.lineno}")
    return sites


def test_no_strict_env_truth_comparisons():
    sites = _strict_sites()
    assert not sites, (
        "env flag parsed with a strict string comparison (`.lower() == 'true'`); "
        "use core.env.bool_env(name, default) / parse_bool(value, default): "
        f"{sites}")
