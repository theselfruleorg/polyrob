"""A third party's INFLOW must never cancel our OUTFLOW (037, found on prod).

`parse_deltas` sums native lamports across every pubkey it is told is ours.
`simulate` used to hand it the FULL address list — every account whose state was
fetched, including the counterparty's. A transfer from the owner INTO a
third-party plain account in that list then nets to roughly zero, and the
SOL-drain assertion passes on a transaction that drained the wallet.

Measured on prod 2026-09-11: owner -900,005,000, Relay vault +900,000,000,
reported native_delta -5,000 — i.e. "only the fee moved" about a transaction
moving 0.9 SOL.
"""
from core.wallet.solana_simulation import parse_deltas

OWNER = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"
VAULT = "7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ"


def _sim(pre_lamports, post_lamports):
    """A plain-account (non-token) pre/post pair per address, owner first."""
    return {
        "_pre": [{"lamports": v, "data": {}} for v in pre_lamports],
        "value": {"accounts": [{"lamports": v, "data": {}} for v in post_lamports],
                  "err": None, "logs": []},
    }


def test_the_counterparty_inflow_cancels_the_drain_when_it_is_called_ours():
    """The BUG, pinned so it cannot come back: label the vault as ours and the
    0.9 SOL drain reads as a 5,000-lamport fee."""
    sim = _sim([988_509_480, 0], [88_504_480, 900_000_000])
    d = parse_deltas(sim, owner=OWNER, owned_pubkeys=[OWNER, VAULT])
    assert d.native_delta == -5_000, "this is the failure mode, not the fix"


def test_naming_only_our_own_account_measures_the_real_drain():
    """The FIX: `ours` excludes the counterparty, so the outflow is visible."""
    sim = _sim([988_509_480, 0], [88_504_480, 900_000_000])
    d = parse_deltas(sim, owner=OWNER, owned_pubkeys=[OWNER])
    assert d.native_delta == -900_005_000


def test_the_split_excludes_third_party_account_keys():
    from core.wallet.solana_tx_inspect import simulation_address_split
    keys = [VAULT, "11111111111111111111111111111111"]
    addresses, ours = simulation_address_split(OWNER, mints=(), rpc=None,
                                               account_keys=keys)
    assert OWNER in addresses and VAULT in addresses
    assert ours == [OWNER], "only our own accounts may be named ours"


def test_ours_never_names_an_account_that_was_dropped_by_the_cap():
    """`ours` must be a subset of what was actually fetched, or parse_deltas
    would index a pre/post pair that is not there."""
    from core.wallet.solana_tx_inspect import simulation_address_split
    keys = [f"k{i}" for i in range(40)]
    addresses, ours = simulation_address_split(OWNER, mints=(), rpc=None,
                                               account_keys=keys)
    assert set(ours) <= set(addresses)
