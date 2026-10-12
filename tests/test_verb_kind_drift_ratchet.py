"""W4 drift ratchet: no hand list of a KIND of verb beside the policy rows.

The outbound, produced-output, late-send and read-job verbs are columns of the
per-action policy rows (``core/verb_policy_rows.py``; a pack's in its
``pack.toml``). Every gate reads a derived view (``tests/unit/core/
test_verb_kind_views.py``). This test fails when a production module defines a
literal collection (set, frozenset, list, tuple or dict keys) that names two or
more of those verbs — the shape the five drifted hand lists had. Add a column
to the row instead, and derive the list.

``ALLOWED``: literals that legitimately name these verbs for another reason,
each with that reason. Shrink-only.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ("core", "modules", "agents", "tools", "api", "cli", "surfaces", "webview",
        "cron", "packs", "polyrob")
#: The sources themselves.
SOURCES = {"core/verb_policy_rows.py"}

#: path -> reason. A literal in one of these files may name the derived verbs.
ALLOWED = {
    "agents/task/agent/core/correspondent_gate.py":
        "_REPLY_ACTIONS: the D1 reply exemption reads call ARGUMENTS (code, per verb_policy doc)",
    "tools/code_exec/tool_rpc.py":
        "the tool-RPC denylist of loop-control verbs, a different property",
    "cli/ui/dialog.py": "the CLI's rendering of the owner dialog (speech vs done), display only",
    "packs/x/polyrob_x/twitter_tool.py":
        "_LATE_MODELS: the late sender's param models; register_late_sender refuses a "
        "name that is not a late_send row",
}


def _derived():
    from core.verb_policy import ids_where
    names = set()
    for pred in (dict(outbound=bool), dict(late_send=True), dict(output=True),
                 dict(read_job=True)):
        names |= ids_where(**pred)
    return names


def _literal_names(node):
    elts = None
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elts = node.elts
    elif isinstance(node, ast.Dict):
        elts = [k for k in node.keys if k is not None]
    if not elts:
        return set()
    return {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _hits():
    derived = _derived()
    out = {}
    for top in SCAN:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SOURCES or "/tests/" in rel or "/__pycache__/" in rel:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                overlap = _literal_names(node) & derived
                if len(overlap) >= 2:
                    out.setdefault(rel, set()).update(overlap)
    return out


def test_no_hand_list_of_a_derived_verb_kind():
    hits = {p: sorted(n) for p, n in _hits().items() if p not in ALLOWED}
    assert not hits, (
        "a literal collection names verbs whose KIND is a policy-row column "
        f"(outbound / text_fields / late_send / output / read_job): {hits}. "
        "Derive it from core.verb_policy.ids_where instead (W4).")


def test_the_allowlist_is_tight():
    hits = _hits()
    stale = sorted(p for p in ALLOWED if p not in hits)
    assert not stale, f"no longer names the derived verbs — delete from ALLOWED: {stale}"
