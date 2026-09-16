"""043 A5 final fix round — `TxNotice.chain` has producers, so the link renders.

⚠️ The defect: `TxNotice.chain` had **zero** producers. Twenty-four `TxNotice(`
sites across `tools/`, `core/`, and nothing passed `chain=`, so
`core/wallet/tx_notify.py::_tx_link` always returned `None` and the explorer
link A5 shipped has never once rendered. Every money notice the owner has ever
received carried a bare hash.

The discipline the field exists for is stricter than "fill it in": **a link only
for the chain the transaction LANDED on**. A wrong-chain link looks
authoritative and resolves to nothing, which is worse than no link — so a site
that cannot name its chain unambiguously passes nothing, and the ratchet below
counts those rather than letting them drift back up.
"""
import pathlib
import re

import pytest

from core.wallet.tx_notify import TxNotice, render_broadcast, render_settled

_REPO = pathlib.Path(__file__).resolve().parents[4]


# --- the link renders at all ------------------------------------------------- #

@pytest.mark.parametrize("render", [render_broadcast, render_settled])
def test_a_notice_with_a_chain_renders_its_explorer_link(render):
    text = render(TxNotice(verb="swap", route="base", tx_ref="0xabc",
                           chain="base"))
    assert "basescan.org/tx/0xabc" in text


@pytest.mark.parametrize("render", [render_broadcast, render_settled])
def test_a_notice_without_a_chain_renders_no_link(render):
    """The pre-fix behaviour of every notice ever sent: no chain, no link."""
    text = render(TxNotice(verb="swap", route="base", tx_ref="0xabc"))
    assert "http" not in text
    assert "0xabc" in text  # …but the hash is still there to chase


def test_an_unknown_chain_renders_no_link():
    text = render_broadcast(TxNotice(verb="swap", route="?", tx_ref="0xabc",
                                     chain="atlantis"))
    assert "http" not in text


def test_a_solana_signature_links_to_solscan():
    text = render_settled(TxNotice(verb="solana_swap", route="solana",
                                   tx_ref="5Kd3Nb", chain="solana"))
    assert "solscan.io/tx/5Kd3Nb" in text


# --- the swap notices, through the code that builds them --------------------- #

def _notice_kwargs_containing(source: str, marker: str) -> str:
    """The argument text of the one ``TxNotice(...)`` call containing *marker*."""
    for match in re.finditer(r"TxNotice\(", source):
        depth, i = 1, match.end()
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        call = source[match.end():i]
        if marker in call:
            return call
    raise AssertionError(f"no TxNotice call contains {marker!r}")


@pytest.mark.parametrize("marker", ["amount_in=amount_in_label",
                                    "amount_out=amount_out_label,\n            usd="])
def test_the_swap_broadcast_and_settled_notices_carry_their_chain(marker):
    """The two notices an owner gets for every EVM swap — the broadcast and the
    settlement — are the ones this link was built for."""
    source = (_REPO / "tools" / "defi" / "trade_tool.py").read_text()
    call = _notice_kwargs_containing(source, marker)
    assert "chain=_route_label" in call, call
    # …and `_route_label` is the intent's own chain, not a rendered label.
    assert "_route_label = intent.chain" in source


def test_the_solana_swap_notices_carry_solana():
    source = (_REPO / "tools" / "defi" / "trade_tool.py").read_text()
    for marker in ('verb="solana_swap", route="solana", chain="solana", usd=amount_usd,\n                tx_ref',
                   'verb="solana_swap", route="solana", chain="solana", usd=amount_usd, tx_ref=signature'):
        assert marker in source


def test_every_bridge_notice_carries_the_ORIGIN_chain():
    """⚠️ Including the ARRIVAL one. `tx_ref` is the origin transaction on every
    bridge notice (`bridge_guard.settle` records the SEND), so a destination
    link would put a Solana base58 signature in an EVM explorer URL — the exact
    failure `polyrob wallet bridges` had. The arrival has no hash of its own; it
    is proven by a measured balance, which the notice states separately."""
    source = (_REPO / "tools" / "defi" / "bridge_verb.py").read_text()
    assert source.count("chain=_origin_chain") == 4
    assert '_origin_chain = (params.from_chain or "").strip().lower() or None' in source
    assert "chain=dest_name" not in source


def test_the_watcher_names_the_origin_and_never_the_destination():
    source = (_REPO / "core" / "wallet" / "bridge_watcher.py").read_text()
    assert source.count("chain=origin_name") == 3
    assert 'origin_name = bridge_guard.chain_name_for_id(row.get("origin_chain_id"))' in source
    assert "chain=chain_name" not in source


# --- the ratchet -------------------------------------------------------------- #

def _notice_sites():
    """Every non-test ``TxNotice(`` call, and whether it names its chain."""
    out = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or rel.startswith("build/"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"TxNotice\(", text):
            depth, i = 1, match.end()
            while i < len(text) and depth:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            call = text[match.end():i]
            line = text[:match.start()].count("\n") + 1
            out.append((f"{rel}:{line}", bool(re.search(r"\bchain\s*=", call))))
    return out


#: Sites that do NOT name a chain. **SHRINK-ONLY.** A site belongs here only
#: when the chain the transaction landed on is genuinely not in scope — never
#: because filling it in was awkward. It is currently empty, which is the point:
#: all 24 producers name their chain.
NO_CHAIN_ALLOWED: frozenset = frozenset()


def test_the_scan_sees_the_real_producers():
    """A ratchet over an empty set is a ratchet nobody has seen work."""
    sites = _notice_sites()
    assert len(sites) >= 20, len(sites)
    files = {s.split(":")[0] for s, _ in sites}
    assert "tools/defi/trade_tool.py" in files
    assert "core/wallet/bridge_watcher.py" in files


def test_no_new_notice_forgets_its_chain():
    naked = sorted(site for site, has in _notice_sites()
                   if not has and site not in NO_CHAIN_ALLOWED)
    assert not naked, (
        "these TxNotice sites name no chain, so their explorer link cannot "
        "render: " + ", ".join(naked) + ". Pass chain=<registry key> for the "
        "chain the transaction LANDED on, or add the site to NO_CHAIN_ALLOWED "
        "with the reason it genuinely cannot know.")


def test_the_allowlist_only_shrinks():
    """A row for a site that now names its chain is a row nobody will clear."""
    naked = {site for site, has in _notice_sites() if not has}
    stale = sorted(NO_CHAIN_ALLOWED - naked)
    assert not stale, f"delete these rows, they name a chain now: {stale}"
