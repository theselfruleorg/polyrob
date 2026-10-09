"""The signer's owner review decodes who the transaction pays and hides no address."""
import json
import types

from core.signer.review import evm_review
from core.wallet.tx_guard import TxIntent

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ALICE = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
MALLORY = "0x3333333333333333333333333333333333333333"
DECISION = types.SimpleNamespace(amount_usd=1.0, reason="ok")


def _review(intent, tx):
    tx = {"gas": 100_000, "maxFeePerGas": 1, "value": 0, **tx}
    return json.loads(evm_review(intent, tx, DECISION, digest="d", unpriced_assets=False))


def test_the_decoded_recipient_is_shown_and_a_mismatch_is_flagged():
    intent = TxIntent(chain="base", token=USDC, to=ALICE, amount_raw=1, max_spend_usd=1.0)
    data = "0xa9059cbb" + "0" * 24 + MALLORY[2:] + f"{1:064x}"
    payee = _review(intent, {"to": USDC, "data": data})["decoded_payee"]
    assert payee["recipient"] == MALLORY and payee["differs_from_declared_to"] is True


def test_an_address_past_the_shown_words_is_still_listed():
    intent = TxIntent(chain="base", token=None, to=USDC, amount_raw=0, max_spend_usd=1.0)
    words = ["0" * 64] * 31 + ["0" * 24 + MALLORY[2:]]
    review = _review(intent, {"to": USDC, "data": "0x12345678" + "".join(words)})
    assert review["address_words_beyond"] == {"31": MALLORY}


def test_a_long_declared_value_is_shown_by_hash_so_a_deploy_stays_reviewable():
    intent = TxIntent(chain="base", token=None, to=None, amount_raw=0, max_spend_usd=1.0,
                      is_deploy=True, init_code="0x" + "60" * 20_000)
    review = _review(intent, {"to": None, "data": "0x" + "60" * 20_000})
    assert review["declared_intent"]["init_code"]["chars"] == 40_002
    assert review["calldata_words"] == []


def test_the_signer_finds_a_cap_refusal_by_the_structured_flag_not_the_text():
    from core.signer.server import SignerService
    from core.wallet.tx_guard import Decision
    text_only = Decision(False, "refused by PolicyGate: daily spend cap $1 would be exceeded",
                         amount_usd=5.0)
    flagged = Decision(False, "refused by PolicyGate: anything", amount_usd=5.0, cap_exceeded=True)
    assert SignerService._cap_refusal(text_only) is False
    assert SignerService._cap_refusal(flagged) is True


def test_the_ledger_flags_only_cap_refusals():
    from core.money.ledger import SpendLedger
    gate = SpendLedger(max_per_tx_usd=10.0, daily_cap_usd=20.0)
    over = gate.check(venue="defi", amount_usd=11.0, idempotency_key=None)
    assert not over.allowed and over.cap_exceeded is True
    bad = gate.check(venue="defi", amount_usd=float("nan"), idempotency_key=None)
    assert not bad.allowed and bad.cap_exceeded is False
