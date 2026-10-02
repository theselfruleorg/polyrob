"""067 P1b ratchet: the money refusals are composed ONLY by the kernel.

``leaf_refusal`` / ``turn_refusal`` / ``spend_pause_refusal`` (and the leaf
half ``delegated_refusal``) are called only inside ``core/money/``; every
other caller asks ``core.money.authorize.authorize_spend``. The allowlist
below names the callers that were NOT switched and why. Shrink-only: lower a
count (or delete a row) when a caller moves to the kernel; never raise one.

Also pinned: no second ``money_action`` / ``money_tool`` predicate outside
``core/money/classify.py``.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[4]
PREDICATES = {"leaf_refusal", "turn_refusal", "spend_pause_refusal", "delegated_refusal"}
SKIP_PARTS = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
              "build", "dist", "docs", "scripts"}

#: path -> (max call count, why it is not switched). Shrink-only.
ALLOWED = {
    "core/wallet/tx_guard.py": (1, (
        "tx_guard steps 1-2 run pause before turn origin with injected probes "
        "(halted_fn, entry_paused_fn — the signer and tests inject them), an "
        "exit-shaped entry-pause exemption keyed on the TxIntent, the monitor-exit "
        "and autonomous lanes that feed step 9, and refusal telemetry; the kernel "
        "order (principal first) and sentences differ")),
    "packs/markets/polyrob_markets/trade_gate.py": (2, (
        "trade_turn_refusal refuses a forged turn BEFORE the principal; the kernel "
        "runs the principal first. Decisions are identical (162 combinations "
        "checked) but a forged turn of a non-owner would read the principal "
        "sentence, and tests pin the forged one. Its halt/entry bars keep the "
        "owner-only risk_reducing carve-out and the legacy kill-switch sentence")),
    "tools/defi/trade_tool.py": (2, (
        "the non-route grant gate prefixes the principal sentence with its own "
        "cause, and _solana_turn_gate is the SVM mirror of tx_guard steps 1-2 "
        "(same reason as tx_guard)")),
    "tools/defi/data_tool.py": (1, (
        "_operator_read_refusal gates a READ of the operator wallet, not a spend")),
}


def _calls(path: pathlib.Path) -> int:
    n = 0
    for node in ast.walk(ast.parse(path.read_text(errors="ignore"))):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name in PREDICATES:
                n += 1
    return n


def _py_files():
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO)
        if SKIP_PARTS.intersection(rel.parts):
            continue
        yield rel, p


def test_refusals_are_composed_only_by_the_kernel():
    found = {}
    for rel, p in _py_files():
        if rel.parts[:2] == ("core", "money"):
            continue
        n = _calls(p)
        if n:
            found[rel.as_posix()] = n
    over = {k: v for k, v in found.items() if v > ALLOWED.get(k, (0, ""))[0]}
    assert not over, (
        f"money refusals called outside core/money/: {over}. Ask "
        f"core.money.authorize.authorize_spend instead.")


def test_the_allowlist_only_shrinks():
    """A row whose file no longer calls a predicate must be deleted."""
    stale = {k: v[0] for k, v in ALLOWED.items()
             if _calls(REPO / k) < v[0]}
    assert not stale, f"lower these allowlist counts (the callers moved): {stale}"


def test_one_money_predicate():
    defs = []
    for rel, p in _py_files():
        if rel.as_posix() == "core/money/classify.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(errors="ignore"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name in {"money_action", "money_tool"}:
                defs.append(f"{rel}:{node.lineno}")
    assert not defs, f"a second money predicate: {defs}"
