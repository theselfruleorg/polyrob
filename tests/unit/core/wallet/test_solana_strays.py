"""A wallet that DERIVES many accounts and READS one goes blind to its own money.

The incident (2026-08-25 -> 2026-08-28): scripts/x402_solana_roundtrip.py needs
a counterparty, so it derives a "payer" at index 1 and prints a funding request
for it. That request was read as "fund the agent" and received 1 SOL. Nothing in
the running system reads index 1 — portfolio, wallet_status, reconcile,
treasury_balance_usd and solana_swap all use index 0 — so for three days the
agent truthfully reported "gas (SOL): 0, nothing can be sent from here" while
holding ~$200 of SOL one derivation index away.

Nothing was ever lost: the key derives from the same seed. What failed is that
an empty canonical account is INDISTINGUISHABLE from a broke agent, so the blind
spot was silent. These tests keep it loud.
"""
import pytest

from core.wallet.solana_strays import (StrayAccount, find_stray_accounts,
                                       format_stray_warning)

CANONICAL = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"
STRAY = "6ZRiHb2YbrYWVWtSJwyAi5CmCfbj3spuxjFsUTbK8Mak"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class _Wallet:
    """Derives a distinct address per index, like the real signer."""

    def __init__(self, addresses):
        self._addresses = addresses

    def solana_signer(self, account=0):
        try:
            addr = self._addresses[account]
        except (IndexError, KeyError):
            raise ValueError(f"no account {account}")
        return type("S", (), {"address": addr})()


def _wallet():
    return _Wallet({0: CANONICAL, 1: STRAY, 2: "acct2", 3: "acct3"})


def test_the_actual_incident_is_detected():
    """Index 1 holding 0.9928 SOL + USDC, index 0 empty of SOL."""
    strays = find_stray_accounts(
        _wallet(), depth=3,
        native_fn=lambda a: 0.99281244 if a == STRAY else 0.0,
        tokens_fn=lambda a: {USDC: 192883} if a == STRAY else {})
    assert len(strays) == 1
    assert strays[0].index == 1 and strays[0].address == STRAY
    assert strays[0].sol == pytest.approx(0.99281244)


def test_the_agents_own_account_is_never_stray():
    """Index 0 IS the agent. Value there is not misplaced, and reporting it as
    stray would train the owner to ignore this warning."""
    strays = find_stray_accounts(
        _wallet(), depth=3,
        native_fn=lambda a: 5.0 if a == CANONICAL else 0.0,
        tokens_fn=lambda a: {})
    assert strays == []


def test_a_clean_wallet_reports_nothing():
    strays = find_stray_accounts(_wallet(), depth=3,
                                 native_fn=lambda a: 0.0,
                                 tokens_fn=lambda a: {})
    assert strays == []
    assert format_stray_warning(strays, CANONICAL) is None


def test_rounding_dust_does_not_raise_an_alarm():
    """A closed token account leaves a few lamports behind. Alarming on that
    would make the warning noise, and a noisy warning gets ignored."""
    strays = find_stray_accounts(_wallet(), depth=3,
                                 native_fn=lambda a: 0.00001,
                                 tokens_fn=lambda a: {})
    assert strays == []


def test_a_token_balance_alone_is_enough_to_flag():
    """SPL value with zero SOL is still money in the wrong account."""
    strays = find_stray_accounts(
        _wallet(), depth=3, native_fn=lambda a: 0.0,
        tokens_fn=lambda a: {USDC: 5_000_000} if a == STRAY else {})
    assert [s.index for s in strays] == [1]


def test_a_zero_token_amount_is_not_value():
    strays = find_stray_accounts(_wallet(), depth=3,
                                 native_fn=lambda a: 0.0,
                                 tokens_fn=lambda a: {USDC: 0})
    assert strays == []


def test_the_scan_is_fail_open_on_rpc_and_derivation_errors():
    """A hiccup must not break wallet_status for every caller. A missed warning
    is bad; a wallet verb that raises is worse."""
    def boom(_a):
        raise RuntimeError("rpc down")

    wallet = _Wallet({0: CANONICAL, 1: STRAY})  # index 2/3 raise on derive
    strays = find_stray_accounts(wallet, depth=3, native_fn=boom,
                                 tokens_fn=boom)
    assert strays == []


def test_the_warning_names_the_address_the_money_should_be_at():
    """The owner has to act on this, so it must say WHERE, not just 'somewhere
    else'."""
    warning = format_stray_warning(
        [StrayAccount(index=1, address=STRAY, sol=0.99281244,
                      tokens={USDC: 192883})],
        CANONICAL)
    assert STRAY in warning
    assert CANONICAL in warning
    assert "0.992812440" in warning
    # It must not read as "your money is gone".
    assert "nothing is lost" in warning.lower()
