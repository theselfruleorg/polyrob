"""The card's amount block fits, and never invites a round-number payment.

⚠️ Live, Playground Env 2026-09-15. A PNL ban offer rendered its headline as
``3500.0000000000000000…`` running off the right edge of the card. The true
figure is ``3500.000000000000000004`` — 18 decimals, because the amount jitter
nudges the last raw units so two same-price offers can never share an amount.

That is a money defect, not a cosmetic one. Settlement matches the raw integer
EXACTLY, so a payer who reads the truncated headline and sends a round 3500
gets `payment_unmatched`: their tokens arrive, no offer settles, nobody is
banned, and the room holds money against nothing.

The owner chose a short headline with the exact figure below it. That choice
makes the ``≈`` marker and the EXACT line load-bearing — without them a short
headline is precisely the invitation the truncation was.
"""
import pytest

pytest.importorskip("PIL")

from modules.pfp.cards import _amount_block_lines


DEC = 18
JITTERED = 3500 * 10 ** DEC + 4          # what the rail actually mints


def _lines(raw=JITTERED, dec=DEC, sym="PNL", usd="0.25"):
    return _amount_block_lines(raw=raw, decimals=dec, symbol=sym, amount_usd=usd)


# --- the headline ----------------------------------------------------------

def test_the_headline_is_short_and_marked_approximate():
    head = _lines()["headline"]
    assert head.startswith("≈"), f"no approximation marker: {head!r}"
    assert "3,500" in head and "PNL" in head
    assert len(head) < 24, f"headline still long enough to truncate: {head!r}"


def test_the_headline_never_carries_the_full_precision():
    """That string is what overflowed the card."""
    assert "000000000000000004" not in _lines()["headline"]


# --- what stops the round-number mistake -----------------------------------

def test_the_exact_amount_is_present_in_full():
    block = _lines()
    assert "3500.000000000000000004" in block["exact"]
    assert "PNL" in block["exact"]


def test_the_exact_line_is_labelled_as_mandatory():
    """⚠️ A quiet 'exact:' under an approximate headline is the invitation. It
    must SAY that this figure, not the rounded one, is what to send."""
    label = _lines()["exact_label"].lower()
    assert "exact" in label
    assert any(w in label for w in ("send", "must")), label


def test_a_round_amount_still_shows_an_exact_line():
    """No jitter today does not mean no jitter tomorrow — the payer must learn
    one habit, not two."""
    block = _lines(raw=3500 * 10 ** DEC)
    assert "3500" in block["exact"]


# --- the unchanged dollar-pegged path --------------------------------------

def test_usdc_is_unchanged():
    block = _lines(raw=250_000, dec=6, sym="USDC", usd="0.25")
    assert block["headline"] == "$0.25 USDC"
    assert block["exact"] == ""          # nothing to disambiguate


def test_the_usd_annotation_is_carried():
    assert "0.25" in _lines()["usd"]
