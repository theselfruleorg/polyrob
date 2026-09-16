"""`native_delta` must actually be measured, not structurally zero.

Prod 2026-09-08: every SOL sell refused with "no SOL leaving — native delta 0
lamports". The owner's account IS in the simulated address set and the lamport
data IS in the pre/post state; `simulate()` simply never told `parse_deltas`
which accounts were ours, so the native branch could not run.

The consequence was not only a blocked swap. `is_plausible_rent(native_delta)`
was being handed 0 on every transaction, so it returned True unconditionally --
the SOL-drain assertion has never once fired. That is the EVM bug
(`core/wallet/simulation.py`: "dead code shaped like a defense") reintroduced on
the Solana side.
"""
import pytest

from core.wallet.solana_simulation import parse_deltas

OWNER = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _sim(pre_lamports, post_lamports, *, token_post=None):
    """One system account (ours) plus an optional token account."""
    pre = [{"lamports": pre_lamports, "data": {}}]
    post = [{"lamports": post_lamports, "data": {}}]
    if token_post is not None:
        acct = {"lamports": 2_039_280,
                "data": {"parsed": {"info": {
                    "owner": OWNER, "mint": USDC,
                    "tokenAmount": {"amount": str(token_post)}}}}}
        pre.append(None)
        post.append(acct)
    return {"_pre": pre, "value": {"err": None, "accounts": post}}


def test_native_delta_is_measured_when_the_account_is_declared_ours():
    d = parse_deltas(_sim(1_000_000_000, 70_000_000),
                     owner=OWNER, owned_pubkeys=[OWNER])
    assert d.ok
    assert d.native_delta == -930_000_000, (
        "the owner's lamport change must be measured, not dropped")


def test_owned_pubkeys_cannot_be_omitted():
    """The regression guard for the ACTUAL prod bug, enforced at the signature.

    Omitting the list used to be legal and silently returned native_delta=0,
    which every downstream check read as a measured "no SOL moved" -- so a
    0.93 SOL sell refused as unobservable while `is_plausible_rent(0)` waved
    through every transaction. Making the argument REQUIRED is what turns that
    from a thing you must remember into a thing you cannot forget.
    """
    import inspect

    param = inspect.signature(parse_deltas).parameters["owned_pubkeys"]
    assert param.default is inspect.Parameter.empty, (
        "owned_pubkeys must stay REQUIRED: a default lets a caller silently "
        "disarm native-lamport accounting, which is the prod 2026-09-08 bug")
    with pytest.raises(TypeError):
        parse_deltas(_sim(1_000_000_000, 70_000_000), owner=OWNER)


def test_simulate_names_the_accounts_it_observed():
    """`simulate()` is the only production caller, and it must pass the SAME
    address set it asked the RPC about -- owner first. If it ever stops, the
    native branch goes dark again and nothing else would notice."""
    import inspect

    from core.wallet import solana_tx_inspect

    src = inspect.getsource(solana_tx_inspect.simulate)
    assert "owned_pubkeys=addresses" in src, (
        "simulate() must hand parse_deltas the addresses it observed; "
        "without it native_delta is a measured-looking 0 on every transaction")
