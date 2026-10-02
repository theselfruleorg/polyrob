"""067 P1 ratchet: the migrated per-action lists hold no action-name literals.

Each list below used to be a hand-kept literal and is now a derived view of the
ONE per-action policy table (``core/verb_policy.py``, rows in
``core/verb_policy_rows.py``). A literal collection of action names re-appearing
in one of these modules is a second source of truth: classify the verb in the
rows instead.

Mechanism: parse each module and flag any set/list/tuple/dict literal (or
``frozenset({...})``-style call) that holds 2+ string constants naming a
verb-policy row, where the row name is not also a tool id (tool-id sets such as
``ROOM_FORBIDDEN_TOOL_IDS`` are per-TOOL policy, not per-action). ``ALLOWED``
names the literals that stay code on purpose, each with its reason.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MIGRATED_MODULES = (
    "core/effects.py",
    "core/config_policy/spend_lane.py",
    "core/config_policy/payment_tools.py",
    "tools/controller/approval.py",
    "core/surfaces/room_policy.py",
    "agents/task/agent/core/correspondent_gate.py",
)

#: (module, assigned name) -> why the literal is code, not a row.
ALLOWED = {
    ("agents/task/agent/core/correspondent_gate.py", "_HIGH_IMPACT_VERB_SUBSTRINGS"):
        "the substring layer: SUBSTRINGS matched inside any name, not action names "
        "(kept as defense-in-depth; its anchors are reserved rows)",
    ("agents/task/agent/core/correspondent_gate.py", "_REPLY_ACTIONS"):
        "the D1 reply exemption reads call ARGUMENTS (surface/target/to) for exactly "
        "these two verbs; argument-dependent logic stays code (067 §4)",
}

_THRESHOLD = 2


def _action_names() -> set:
    from core.tool_capabilities import TOOL_CAPABILITIES
    from core.verb_policy import VERB_POLICY
    return set(VERB_POLICY) - set(TOOL_CAPABILITIES)


def _literals(tree: ast.AST):
    """Yield (assigned name or '', literal node) for every collection literal."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
            continue
        owner, cur = "", node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.Assign, ast.AnnAssign)):
                targets = cur.targets if isinstance(cur, ast.Assign) else [cur.target]
                owner = next((t.id for t in targets if isinstance(t, ast.Name)), "")
                break
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                break
        yield owner, node


def _strings(node) -> list:
    elts = node.keys if isinstance(node, ast.Dict) else node.elts
    return [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def test_no_action_name_literal_in_a_migrated_module():
    names = _action_names()
    offenders = []
    for rel in MIGRATED_MODULES:
        tree = ast.parse((REPO / rel).read_text())
        for owner, node in _literals(tree):
            if (rel, owner) in ALLOWED:
                continue
            hits = sorted(set(_strings(node)) & names)
            if len(hits) >= _THRESHOLD:
                offenders.append(f"{rel}:{node.lineno} {owner or '<expr>'}: {hits[:6]}")
    assert offenders == [], (
        "action-name literals in a module whose lists are views of the verb-policy "
        "table — add the verbs to core/verb_policy_rows.py instead:\n  "
        + "\n  ".join(offenders))


def test_allowlist_rows_still_exist():
    stale = []
    for rel, owner in ALLOWED:
        tree = ast.parse((REPO / rel).read_text())
        if not any(o == owner for o, _ in _literals(tree)):
            stale.append((rel, owner))
    assert stale == [], f"ALLOWED rows whose literal is gone — delete them: {stale}"


def test_the_ratchet_sees_a_planted_literal():
    """Self-check: the scan flags the shape it exists to catch."""
    tree = ast.parse('X = frozenset({"defi_trade_swap", "message", "git_push"})')
    found = [set(_strings(n)) & _action_names() for _, n in _literals(tree)]
    assert any(len(f) >= _THRESHOLD for f in found)
