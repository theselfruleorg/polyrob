"""Seat-procedure ratchet (proposal 125 S2, review finding A-3).

A seat (``cli/``, ``webview/``, ``api/``, every other ``surfaces/<name>/``) renders; the
procedure of an owner verb lives BELOW the seats, where the agent and every seat reach it.
Today many seats reach the procedure through the Telegram seat's ``surfaces.telegram.*``
modules instead. This ratchet freezes those edges:

1. No NEW edge from another seat into ``surfaces.telegram`` (an import, top-level or lazy,
   or a ``"surfaces.telegram.x:attr"`` string reference).
2. No stale row: an edge that a slice removed must leave ``ALLOWED_TELEGRAM_EDGES``.
3. No NEW private-name import (``from surfaces.telegram.x import _name``): a private name is
   the Telegram seat's internals, never a shared seam.

The fix for a failure is never a new row: move the decision body to the verb's tier
(``tools/defi/owner_verbs/``, ``core/``, ``cron/``) and let both seats render it.
"""
import ast
import re
from pathlib import Path

from tests.support.repo_index import repo_index

REPO_ROOT = Path(__file__).resolve().parent.parent
TELEGRAM = "surfaces.telegram"

# Permanent, reasoned exemptions (not burn-down rows).
EXEMPT = frozenset({
    # The launcher: `polyrob telegram` starts the Telegram seat itself.
    ("cli/commands/telegram.py", "surfaces.telegram.harness"),
    # The surface actor registry: importing the harness registers the core actor.
    ("surfaces/_actor.py", "surfaces.telegram.harness"),
})

# (seat file, surfaces.telegram module). May only SHRINK. Proposal 125 S14 burned every
# row down (the last: /dapp, the reads, the rooms); the set stays empty.
ALLOWED_TELEGRAM_EDGES = frozenset({
})

# (seat file, "surfaces.telegram.module:_private_name"). Frozen; may only SHRINK.
ALLOWED_PRIVATE_IMPORTS = frozenset({
})

_STRING_REF_RE = re.compile(r"^surfaces\.telegram(?:\.[A-Za-z_]\w*)+(?::[A-Za-z_]\w*)?$")


def _seat_files():
    """Every tracked seat .py file: cli/, webview/, api/, surfaces/<other>/, surfaces/*.py."""
    out = []
    for rel in repo_index().tracked():
        if not rel.endswith(".py") or rel.startswith("build/"):
            continue
        parts = rel.split("/")
        if parts[0] in ("cli", "webview", "api"):
            out.append(rel)
        elif parts[0] == "surfaces" and (len(parts) == 2 or parts[1] != "telegram"):
            out.append(rel)
    return out


def _is_telegram(mod: str) -> bool:
    return mod == TELEGRAM or mod.startswith(TELEGRAM + ".")


def _scan():
    edges, private = set(), set()
    for rel in _seat_files():
        if not (REPO_ROOT / rel).exists():
            continue
        for node in repo_index().nodes(REPO_ROOT / rel):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "surfaces":
                    for a in node.names:
                        if a.name == "telegram":
                            edges.add((rel, TELEGRAM))
                elif node.module == TELEGRAM:
                    for a in node.names:
                        edges.add((rel, f"{TELEGRAM}.{a.name}"))
                elif _is_telegram(node.module):
                    edges.add((rel, node.module))
                    for a in node.names:
                        if a.name.startswith("_"):
                            private.add((rel, f"{node.module}:{a.name}"))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if _is_telegram(a.name):
                        edges.add((rel, a.name))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _STRING_REF_RE.match(node.value):
                    mod, _, attr = node.value.partition(":")
                    edges.add((rel, mod))
                    if attr.startswith("_"):
                        private.add((rel, node.value))
    return edges, private


def test_no_new_seat_edge_into_telegram():
    edges, _ = _scan()
    new = sorted(edges - ALLOWED_TELEGRAM_EDGES - EXEMPT)
    assert not new, (
        "A seat reaches into the Telegram seat for an owner-verb procedure. A seat holds no "
        "procedure: move the decision body below the seats (tools/defi/owner_verbs/, core/, "
        f"cron/) and render it from both seats. Do not add a row:\n{new}"
    )


def test_no_stale_seat_edge_rows():
    edges, _ = _scan()
    stale = sorted((ALLOWED_TELEGRAM_EDGES | EXEMPT) - edges)
    assert not stale, f"Delete these fixed rows from the seat-procedure ratchet:\n{stale}"


def test_no_new_private_name_import():
    _, private = _scan()
    new = sorted(private - ALLOWED_PRIVATE_IMPORTS)
    stale = sorted(ALLOWED_PRIVATE_IMPORTS - private)
    assert not new, (
        "A seat imports a PRIVATE name of the Telegram seat. A private name is not a seam; "
        f"move the helper below the seats:\n{new}"
    )
    assert not stale, f"Delete these fixed rows from ALLOWED_PRIVATE_IMPORTS:\n{stale}"

