"""Only seat code may build an owner-shaped execution context (owner-UX review A1).

Since 2026-09-26 a genuine owner turn is authorization: ``owner_direct_turn``
(core/money/authority.py) lets it pass the pause and the autonomous ceiling. The
predicate trusts the context's shape — a non-leaf, non-sub-agent role whose
principal is the owner. So a context built with ``role="owner"`` or
``role="orchestrator"`` by a module that is NOT an owner seat would be a way to
reach owner authority without the owner.

This ratchet lists every place in the product tree that builds such a context
from a literal (a ``role=`` keyword, a ``{"role": ...}`` dict, or a
``getattr(ctx, "role", <owner-shaped default>)`` that treats a MISSING role as
owner-shaped). The set may only shrink. A new entry needs a review: is the file an
owner seat (Telegram, the REPL/CLI, the console login), or the main agent's own
step loop?
"""
import ast
import pathlib

from tests.support.repo_index import repo_index

ROOT = pathlib.Path(__file__).resolve().parent.parent
TREES = ("core", "polyrob", "modules", "agents", "tools", "api", "cli", "surfaces",
         "webview", "cron", "packs", "utils")
OWNER_SHAPED = ("owner", "orchestrator")

#: file -> why it may build an owner-shaped context.
ALLOWED = {
    # The main agent's step loop: its OWN role (set at construction, "leaf" for
    # every sub-agent); turn origin is judged separately (turn_kind, autonomy).
    "agents/task/agent/core/step_execution.py": "the agent's own step context",
    # W5: the ONE owner-seat context constructor (owner_seat_context).
    "core/security/turn_facts.py": "owner_seat_context, called only by seat code",
    "webview/owner_auth.py": "console owner login (auth role, not a turn)",
}

#: W5: the files that CALL the one constructor (``owner_seat_context`` or its
#: ``token_trust.owner_seat_ctx`` delegator) — each an owner seat. A new caller
#: needs the same review a new owner-shaped literal did.
SEAT_CALLERS = {
    "cli/commands/wallet.py": "owner CLI seat (server shell)",
    "core/wallet/token_trust.py": "owner_seat_ctx, the shared /wallet trust replies",
    "surfaces/telegram/nft_ops.py": "Telegram NFT seat",
    "surfaces/telegram/owner_ops.py": "Telegram /bridge seat",
    "surfaces/telegram/send_ops.py": "Telegram /send seat",
    "surfaces/telegram/token_ops.py": "Telegram token seat (/lp, /pay, /check, /claim ride it)",
    "tools/defi/token_identity_ask.py": "the owner's answer to a token-identity ask (/pending)",
    "webview/tokens_routes.py": "console token routes (owner login)",
    "surfaces/telegram/lp_ops.py": "/lp seat (Telegram, the REPL and `polyrob wallet lp`)",
    "packs/markets/polyrob_markets/http_seat.py": "HTTP /execute (non-read verbs need the bound owner)",
}


def _owner_shaped_sites(path: pathlib.Path):
    for node in repo_index().nodes(path):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "role" and isinstance(kw.value, ast.Constant)
                        and kw.value.value in OWNER_SHAPED):
                    yield node.lineno
            if (isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and len(node.args) == 3
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in ("role", "_role")
                    and isinstance(node.args[2], ast.Constant)
                    and node.args[2].value in OWNER_SHAPED):
                yield node.lineno
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "role"
                        and isinstance(v, ast.Constant) and v.value in OWNER_SHAPED):
                    yield node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and target.attr in ("role", "_role")
                        and isinstance(node.value, ast.Constant)
                        and node.value.value in OWNER_SHAPED):
                    yield node.lineno


def _scan():
    found = {}
    for tree in TREES:
        for path in (ROOT / tree).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
                continue
            lines = list(_owner_shaped_sites(path))
            if lines:
                found[rel] = lines
    return found


def test_only_seat_code_builds_an_owner_shaped_context():
    found = _scan()
    new = {f: lines for f, lines in found.items() if f not in ALLOWED}
    assert not new, (
        "a new module builds an owner-shaped execution context; owner_direct_turn "
        "would treat it as the owner acting (pause + autonomous ceiling lifted). "
        f"Review and add it to ALLOWED only if it is an owner seat: {new}")


def test_the_allowlist_only_shrinks():
    stale = sorted(set(ALLOWED) - set(_scan()))
    assert not stale, f"no longer builds an owner-shaped context — remove from ALLOWED: {stale}"


def _seat_callers():
    found = set()
    for tree in TREES:
        for path in (ROOT / tree).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{rel}" or rel in ALLOWED or rel == "core/security/owner_turn.py":
                continue
            for node in repo_index().nodes(path):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else (
                        f.attr if isinstance(f, ast.Attribute) else None)
                    if name in ("owner_seat_context", "owner_seat_ctx"):
                        found.add(rel)
                        break
    return found


def test_only_seat_code_calls_the_seat_constructor():
    found = _seat_callers()
    new = sorted(found - set(SEAT_CALLERS))
    assert not new, f"a new module builds an owner-seat context; review it: {new}"
    stale = sorted(set(SEAT_CALLERS) - found)
    assert not stale, f"no longer calls the seat constructor — remove: {stale}"
