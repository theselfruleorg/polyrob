"""Solana simulation + the SPL threat taxonomy. Phase 3.

`tx_guard`'s EVM step-6 defense is built on ERC-20 standing allowances:
an undeclared `Approval` in the logs is refused. **That does not port.** Jupiter
needs no standing allowance, so `approve -> swap -> revoke` is meaningless here,
and SPL's real drain vectors are different in kind:

  * `SetAuthority` — hands the token account (or mint) to someone else
  * `CloseAccount` — drains the account and reclaims its rent
  * freeze / mint authority — makes the position unsellable or dilutes it
  * any undeclared write to an account we own

So the invariant is RE-DERIVED, not translated: "hidden approve = future drain"
becomes "hidden authority change / account close / undeclared owned-account
write". These tests pin that taxonomy against the post-state simulation returns.
"""
import pytest

from core.wallet import solana_simulation as ss

ME = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER = "7WktogJEd2wQ9eH2oWusmcoFTgeYi6rS632UviTBJ2jm"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _acct(owner=ME, mint=USDC, amount="1000000", delegate=None,
          close_authority=None, state="initialized", program=TOKEN_PROGRAM):
    info = {"owner": owner, "mint": mint,
            "tokenAmount": {"amount": amount, "decimals": 6},
            "state": state}
    if delegate:
        info["delegate"] = delegate
    if close_authority:
        info["closeAuthority"] = close_authority
    return {"owner": program,
            "data": {"parsed": {"type": "account", "info": info}}}


def _sim(pre, post, err=None, logs=None):
    return {"value": {"err": err, "logs": logs or [],
                      "accounts": post, "unitsConsumed": 50_000},
            "_pre": pre}


# -- the simulation must actually run ---------------------------------------

def test_a_failing_simulation_is_not_ok():
    d = ss.parse_deltas(_sim([], [], err={"InstructionError": [0, "Custom"]}), owner=ME)
    assert d.ok is False
    assert d.reason


def test_an_unreadable_simulation_is_not_ok():
    """No post-state means nothing can be asserted. Fail closed."""
    assert ss.parse_deltas(None, owner=ME).ok is False
    assert ss.parse_deltas({"value": None}, owner=ME).ok is False


# -- balance deltas ----------------------------------------------------------

def test_a_token_outflow_is_measured():
    pre = [_acct(amount="3000000")]
    post = [_acct(amount="1000000")]
    d = ss.parse_deltas(_sim(pre, post), owner=ME)
    assert d.ok is True
    assert d.token_deltas[USDC] == -2_000_000


def test_a_token_inflow_is_measured():
    d = ss.parse_deltas(_sim([_acct(amount="0")], [_acct(amount="500")]), owner=ME)
    assert d.token_deltas[USDC] == 500


def test_accounts_we_do_not_own_are_ignored():
    """Someone else's balance moving is not our delta."""
    pre = [_acct(owner=OTHER, amount="9")]
    post = [_acct(owner=OTHER, amount="0")]
    assert ss.parse_deltas(_sim(pre, post), owner=ME).token_deltas == {}


# -- the SPL threat taxonomy (the part that does NOT port from EVM) ----------

def test_a_new_delegate_on_our_account_is_flagged():
    """SPL's closest thing to a standing allowance. Undeclared = refuse."""
    d = ss.parse_deltas(_sim([_acct()], [_acct(delegate=OTHER)]), owner=ME)
    assert ("delegate", USDC, OTHER) in d.authority_grants


def test_a_changed_account_owner_is_flagged_as_a_drain():
    """SetAuthority handing our token account to someone else. There is no EVM
    equivalent — the account simply stops being ours."""
    d = ss.parse_deltas(_sim([_acct()], [_acct(owner=OTHER)]), owner=ME)
    assert any(k == "owner_changed" for k, *_ in d.authority_grants)


def test_a_new_close_authority_is_flagged():
    """CloseAccount drains the balance AND reclaims rent — a drain primitive
    with no EVM analogue."""
    d = ss.parse_deltas(_sim([_acct()], [_acct(close_authority=OTHER)]), owner=ME)
    assert any(k == "close_authority" for k, *_ in d.authority_grants)


def test_an_account_becoming_frozen_is_flagged():
    """Frozen means unsellable. The Solana shape of a honeypot."""
    d = ss.parse_deltas(_sim([_acct()], [_acct(state="frozen")]), owner=ME)
    assert any(k == "frozen" for k, *_ in d.authority_grants)


def test_a_disappearing_account_is_flagged_as_closed():
    d = ss.parse_deltas(_sim([_acct()], [None]), owner=ME)
    assert any(k == "closed" for k, *_ in d.authority_grants)


def test_an_unchanged_account_grants_nothing():
    d = ss.parse_deltas(_sim([_acct()], [_acct()]), owner=ME)
    assert d.authority_grants == ()
    assert d.grants_authority() is False


def test_grants_authority_is_true_when_anything_was_flagged():
    d = ss.parse_deltas(_sim([_acct()], [_acct(delegate=OTHER)]), owner=ME)
    assert d.grants_authority() is True


# -- rent (mismatch #5: the native-dust assertion breaks here) ---------------

def test_native_lamport_delta_is_measured():
    pre = [{"lamports": 1_000_000_000, "owner": "11111111111111111111111111111111"}]
    post = [{"lamports": 995_000_000, "owner": "11111111111111111111111111111111"}]
    d = ss.parse_deltas(_sim(pre, post), owner=ME, owned_pubkeys=[ME])
    assert d.native_delta == -5_000_000


def test_rent_for_a_new_token_account_is_classified_not_refused():
    """A first-time transfer legitimately spends ~0.002 SOL creating the
    recipient's ATA. An EVM-style 'any native movement above dust is a drain'
    rule would refuse every first transfer to a new counterparty."""
    assert ss.is_plausible_rent(-2_100_000) is True      # ~0.0021 SOL
    assert ss.is_plausible_rent(-500_000_000) is False   # 0.5 SOL is not rent


# --------------------------------------------------------------------------
# A FIRST trade creates the token account (2026-08-26)
#
# Caught on the first real mainnet swap. A wallet that has never held a token
# has no ATA for it, and the swap transaction CREATES one. So the account is
# absent from the pre-state and present in the post-state — and the parser
# skipped exactly that pair, because it required an entry in both. Every first
# trade on a fresh Solana wallet was therefore invisible to the delta check,
# which then refused it as "no token movement".
# --------------------------------------------------------------------------

def test_an_account_created_by_the_transaction_counts_as_a_full_credit():
    """No pre-entry at all: the balance went from nothing to something."""
    d = ss.parse_deltas(_sim([None], [_acct(amount="1500000")]), owner=ME)
    assert d.ok is True
    assert d.token_deltas[USDC] == 1_500_000


def test_a_created_account_owned_by_SOMEONE_ELSE_is_not_our_credit():
    d = ss.parse_deltas(_sim([None], [_acct(owner=OTHER, amount="9")]), owner=ME)
    assert d.token_deltas == {}


def test_a_created_account_is_not_treated_as_an_authority_grant():
    """Creating our own ATA is ordinary, not a drain vector."""
    d = ss.parse_deltas(_sim([None], [_acct(amount="5")]), owner=ME)
    assert d.authority_grants == ()


def test_a_shorter_pre_list_than_post_still_parses():
    """The pre-state can simply be shorter — the RPC returns null for accounts
    that did not exist yet."""
    d = ss.parse_deltas(_sim([], [_acct(amount="7")]), owner=ME)
    assert d.token_deltas[USDC] == 7
