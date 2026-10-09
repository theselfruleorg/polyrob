"""Address poisoning: refuse a payee that only LOOKS like one we already paid.

Prod 2026-10-04 17:46: right after the owner's payments to
0x45Dd976d6E2f4557f2dfcD78FB75Bb19DCC32DC0 and 0x759a0D75fF97673D1242B9ad7421480626F70C82,
dust arrived from 0x45d4dccbe859ed59ba3a46de6eb3ec214f7c2dc0 and
0x759a4f75a99cbaeccd43a3c860cbefd828130c82 — vanity addresses with the same head
and tail, planted in the history so the next payment gets copied to them.
"""
import json

from core.wallet import address_lookalike as al

PAID_A = "0x45Dd976d6E2f4557f2dfcD78FB75Bb19DCC32DC0"
PAID_B = "0x759a0D75fF97673D1242B9ad7421480626F70C82"
FAKE_A = "0x45d4dccbe859ed59ba3a46de6eb3ec214f7c2dc0"
FAKE_B = "0x759a4f75a99cbaeccd43a3c860cbefd828130c82"


def test_the_prod_pairs_are_lookalikes():
    assert al.lookalike_of(FAKE_A, [PAID_A, PAID_B]) == PAID_A
    assert al.lookalike_of(FAKE_B, [PAID_A, PAID_B]) == PAID_B


def test_the_real_payee_itself_is_not_a_lookalike():
    assert al.lookalike_of(PAID_A.lower(), [PAID_A]) is None
    assert al.lookalike_of(PAID_A, [PAID_A, PAID_B]) is None


def test_an_unrelated_address_is_not_a_lookalike():
    assert al.lookalike_of("0x" + "1" * 40, [PAID_A, PAID_B]) is None


def test_non_evm_or_malformed_input_never_matches():
    assert al.lookalike_of("not-an-address", [PAID_A]) is None
    assert al.lookalike_of(FAKE_A, ["garbage", None]) is None


def _ledger(tmp_path, rows):
    p = tmp_path / "audit.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def test_paid_counterparties_reads_transfers_from_the_ledger(tmp_path):
    path = _ledger(tmp_path, [
        {"ts": 1.0, "venue": "defi", "action": "transfer", "counterparty": PAID_A},
        {"ts": 2.0, "venue": "defi", "action": "swap", "counterparty": "0xrouter"},
        {"ts": 3.0, "venue": "defi", "action": "transfer", "counterparty": PAID_B},
        {"action": "other"},
    ])
    assert set(al.paid_counterparties(path=path, now=10.0)) == {PAID_A, PAID_B}


def test_paid_counterparties_honours_the_window(tmp_path):
    path = _ledger(tmp_path, [
        {"ts": 1.0, "venue": "defi", "action": "transfer", "counterparty": PAID_A},
    ])
    assert al.paid_counterparties(path=path, now=1.0 + al.WINDOW_SEC + 1) == []


def test_a_missing_ledger_confirms_nothing(tmp_path):
    assert al.paid_counterparties(path=str(tmp_path / "nope.jsonl")) == []


def test_refusal_names_both_addresses(tmp_path):
    path = _ledger(tmp_path, [
        {"ts": 1.0, "venue": "defi", "action": "transfer", "counterparty": PAID_A},
    ])
    msg = al.poisoning_refusal(FAKE_A, path=path, now=2.0)
    assert msg and FAKE_A in msg and PAID_A in msg
    assert al.poisoning_refusal(PAID_A, path=path, now=2.0) is None


def test_solana_lookalike_keeps_base58_case_and_reads_solana_transfers(tmp_path):
    paid = "AbC" + "1" * 25 + "XyZ9"
    fake = "AbC" + "2" * 25 + "XyZ9"
    path = _ledger(tmp_path, [
        {"ts": 1.0, "action": "solana_transfer", "counterparty": paid},
    ])
    assert al.lookalike_of(fake, [paid], chain="solana") == paid
    assert al.lookalike_of(fake.lower(), [paid], chain="solana") is None
    assert al.lookalike_of(paid, [paid], chain="solana") is None
    msg = al.poisoning_refusal(fake, path=path, now=2.0, chain="solana")
    assert msg and paid in msg and fake in msg


def test_poisoning_check_fails_closed_on_corrupt_or_unreadable_history(tmp_path):
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json")
    assert "unreadable" in al.poisoning_refusal(PAID_A, path=str(broken))
    assert "unreadable" in al.poisoning_refusal(PAID_A, path=str(tmp_path))


def test_matching_long_prefix_or_suffix_and_small_edits_are_detected():
    body = PAID_A[2:].lower()
    for fake in [body[:8] + "f" * 32, "a" * 32 + body[-8:], "9" + body[1:]]:
        assert al.lookalike_of("0x" + fake, [PAID_A]) == PAID_A


def test_linked_history_refuses(tmp_path):
    import os
    original = tmp_path / "original.jsonl"
    original.write_text("")
    linked = tmp_path / "linked.jsonl"
    os.link(original, linked)
    assert "unreadable" in al.poisoning_refusal(PAID_A, path=str(linked))


def test_nft_sends_count_as_payees_under_their_recorded_action_names(tmp_path):
    """The ledger records an NFT send as ``nft_transfer`` (trade_tool) and an
    agent-NFT withdrawal as ``agent_nft_withdraw_token`` (guarded_call); the
    check read ``nft_send``/``withdraw_token``, names no writer uses."""
    path = _ledger(tmp_path, [
        {"ts": 1.0, "venue": "defi", "action": "nft_transfer", "counterparty": PAID_A},
        {"ts": 1.0, "venue": "defi", "action": "agent_nft_withdraw_token", "counterparty": PAID_B},
    ])
    assert set(al.paid_counterparties(path=path, now=10.0)) == {PAID_A, PAID_B}
