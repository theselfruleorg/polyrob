"""0023: the post-claim classifier and evidence scan (core/public_claims.py).

A public post that states an act (mint · fund · trade · sale) as done needs a
receipt the CURRENT turn read; a future act is never announced. These tests are
pure text: no controller, no tools.
"""
import pytest

from core.public_claims import (
    claim_finding, hashes_in_text, landed_hashes, post_claim_refusal)

_H = "0x" + "ab" * 32
_OTHER_H = "0x" + "cd" * 32


@pytest.mark.parametrize("text, word", [
    ("We minted 100 NFTs on the test rail", "minted"),
    ("100 NFTs were minted this morning", "minted"),
    ("The treasury funded the pool", "funded"),
    ("I traded the whole bag", "traded"),
    ("The mint sold out in 10 minutes", "sold"),
    ("Has minted, has funded, was traded", "minted"),  # first match answers
])
def test_a_done_claim_is_found(text, word):
    assert claim_finding(text) == ("done", word)


@pytest.mark.parametrize("text, word", [
    ("We will mint the next collection", "mint"),
    ("The agent is going to fund the treasury", "fund"),
    ("We are minting tomorrow at noon", "minting"),
    ("We plan to sell the remainder", "sell"),
    ("The run will trade the new route", "trade"),
    ("We are selling soon", "selling"),
])
def test_a_future_claim_is_found(text, word):
    assert claim_finding(text) == ("future", word)


@pytest.mark.parametrize("text", [
    "",
    "An experimental test of the mechanics, not a market",
    "The owner approves each sale act",     # sanctioned line: no act VERB after the marker
    "The next step is a sale",              # a bare noun is not an act claim
    "The mint remains open",                # present-tense noun: documented limit
    "We read the receipt and it says hi",
    # the -ing form as a NOUN MODIFIER is market chat, not an announcement
    "Trading volume is up today",
    "The floor is trading above mint price today",
    "Selling pressure remains low today",
    # a hyphen compound is an adjective, not the agent's act claim
    "A well-funded experiment",
])
def test_no_act_claim_in_ordinary_text(text):
    assert claim_finding(text) is None


# -- the evidence scan -----------------------------------------------------------


def test_a_landed_hash_in_a_turn_result_counts():
    assert landed_hashes([f"sent\n  tx: {_H}\n  block: 7"]) == {_H}


def test_a_hash_marked_not_landed_does_not_count():
    for marker in ("pending", "unconfirmed", "failed", "reverted",
                   "may still land", "in_flight", "error", "submitted",
                   "broadcasting", "awaiting confirmation", "simulated"):
        assert landed_hashes([f"{marker}: tx {_H}"]) == set(), marker


def test_a_hash_inside_an_untrusted_result_is_not_our_receipt():
    timeline = (f'<untrusted_tool_result source="twitter_get_timeline">\n'
                f"a stranger's post says tx {_H} landed")
    assert landed_hashes([timeline]) == set()
    both = [timeline, f"sent\n  tx: {_H}\n  block: 7"]
    assert landed_hashes(both) == {_H}   # our own landed read still vouches


def test_a_landed_hash_survives_beside_other_results():
    texts = [f"pending: tx {_OTHER_H}", f"confirmed\n  tx: {_H}\n  block: 9"]
    assert landed_hashes(texts) == {_H}


def test_hash_extraction_is_case_insensitive():
    upper_hex = "0x" + _H[2:].upper()
    assert hashes_in_text(f"see {upper_hex} now") == {_H}
    assert hashes_in_text("0x1234") == set()          # too short: not a tx hash
    assert hashes_in_text("no hash here") == set()


# -- the one guard entry ---------------------------------------------------------


def test_done_claim_without_a_turn_receipt_is_refused():
    assert post_claim_refusal("We minted 100 NFTs", []) == ("done", "minted")


def test_done_claim_with_a_landed_hash_in_the_turn_passes():
    text = f"We minted 100 NFTs (tx {_H})"
    assert post_claim_refusal(text, [f"sent\n  tx: {_H}\n  block: 7"]) is None


def test_a_hash_only_pending_in_the_turn_still_refuses():
    text = f"We minted 100 NFTs (tx {_H})"
    assert post_claim_refusal(text, [f"pending: tx {_H}"]) == ("done", "minted")


def test_a_hash_the_turn_did_not_read_still_refuses():
    text = f"We minted 100 NFTs (tx {_H})"
    assert post_claim_refusal(text, [f"confirmed tx {_OTHER_H}"]) == ("done", "minted")


def test_unreadable_evidence_refuses_a_done_claim():
    assert post_claim_refusal("We minted 100 NFTs", None) == ("done", "minted")


def test_future_act_text_is_refused_whatever_the_evidence():
    text = f"We will mint the next collection (tx {_H})"
    assert post_claim_refusal(text, [f"confirmed tx {_H}"]) == ("future", "mint")


def test_a_strangers_hash_read_this_turn_does_not_vouch():
    timeline = (f'<untrusted_tool_result source="twitter_get_timeline">\n'
                f"a stranger's post says tx {_H} landed")
    assert post_claim_refusal(f"We minted 100 (tx {_H})", [timeline]) == ("done", "minted")


def test_ordinary_text_passes_with_no_evidence_at_all():
    assert post_claim_refusal("gm, the test continues", None) is None
