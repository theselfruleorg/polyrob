"""No chain read on the event loop: an async verb of ``tools/agent_nft`` calls the
RPC only inside ``asyncio.to_thread`` (2026-11-22 overnight stalls: the loop
blocked ~42 s inside ``core/wallet/onchain.py::_rpc`` — a sync HTTPS round trip —
called ON the loop by the async verbs of ``tools/agent_nft`` (the mint-watch goal
runs; every fact read from the sale now means several RPC handshakes). The house
pattern is ``await asyncio.to_thread(...)`` and most of the package already uses
it. This ratchet keeps it that way: inside an ``async def`` under ``tools/agent_nft``,
a call to the RPC callable or a chain-reading helper must sit inside an
``asyncio.to_thread(...)`` (a lambda body counts — it runs in the worker thread).
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "tools" / "agent_nft"

# Attribute calls that hit the chain (module functions taking the rpc callable),
# and bare names imported for the same purpose. ``encode_*`` is local ABI work.
NETWORK_ATTRS = {
    "read_state", "read_sale", "read_wl_minted", "read_phase", "read_owner",
    "read_sale_ready", "read_owed", "read_settled", "read_clearing",
    "open_approvals", "nft_owners", "attribute", "native_usd", "read_head",
    "due_entries", "identity_agent_id",
}
NETWORK_NAMES = {"rpc", "read_owner", "read_head", "due_entries", "native_usd"}


def _wrapped_ids(fn: ast.AsyncFunctionDef) -> set:
    """ids of every AST node sitting inside an asyncio.to_thread(...) argument."""
    wrapped = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "to_thread":
            for sub in ast.walk(node):
                wrapped.add(id(sub))
    return wrapped


def _offenders(src: str, name: str):
    for fn in (n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef)):
        wrapped = _wrapped_ids(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or id(node) in wrapped:
                continue
            f = node.func
            if (isinstance(f, ast.Name) and f.id in NETWORK_NAMES) or (
                    isinstance(f, ast.Attribute) and f.attr in NETWORK_ATTRS):
                yield f"{name}:{node.lineno} in async {fn.name}(): {ast.unparse(node)[:70]}"


def test_no_chain_reads_on_the_event_loop():
    offenders = [o for p in sorted(PKG.glob("*.py"))
                 for o in _offenders(p.read_text(), p.name)]
    assert not offenders, (
        "on-loop chain reads in tools/agent_nft (wrap each in `await asyncio.to_thread(...)`): "
        + "; ".join(offenders)
    )
