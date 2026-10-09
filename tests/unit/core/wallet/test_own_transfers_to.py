"""'Did we pay X?' is answered from our own audit ledger (intel 2026-10-05 08:10Z).

Prod 2026-10-05: Rob sent 80 USDC to 0x19Be…7da6 at 04:17 on the owner's order;
the row is in wallet/audit.jsonl. At 08:04 Rob found "no record", then told the
owner the payment was made outside its rails. Nothing it could call read the
ledger by recipient.
"""
import json

from core.wallet.trade_index import own_transfers_to
from tools.defi.wallet_intel import render_own_payments

PAYEE = "0x19Be8D93E29Ad69A85E105BF28841fbc50D07da6"
TX = "0x9ae69de18e025098b4bb42ef7eafed42dd045f065e0b07213d353552d5c03037"


def _ledger(tmp_path, rows):
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def _row(**kw):
    base = {"ts": 1791173855.8, "venue": "defi", "action": "transfer", "amount_usd": 80.0,
            "counterparty": PAYEE, "result_ref": TX, "chain": "base", "lane": "owner_direct"}
    base.update(kw)
    return base


def test_finds_our_transfer_to_the_recipient_case_insensitively(tmp_path):
    path = _ledger(tmp_path, [_row(), _row(counterparty="0x" + "1" * 40, result_ref="0xother")])
    rows = own_transfers_to(PAYEE.lower(), path=path)
    assert [r["result_ref"] for r in rows] == [TX]
    assert rows[0]["amount_usd"] == 80.0 and rows[0]["chain"] == "base"


def test_non_transfer_actions_are_not_payments(tmp_path):
    path = _ledger(tmp_path, [_row(action="swap")])
    assert own_transfers_to(PAYEE, path=path) == []


def test_missing_ledger_is_unknown_not_empty(tmp_path):
    assert own_transfers_to(PAYEE, path=str(tmp_path / "nope.jsonl")) is None


def test_render_names_the_payment_with_its_tx():
    text = "\n".join(render_own_payments(PAYEE, [_row()]))
    assert "80" in text and TX in text and "audit ledger" in text


def test_render_says_none_recorded_vs_unreadable():
    assert "no transfer to this address" in "\n".join(render_own_payments(PAYEE, []))
    assert "could not be read" in "\n".join(render_own_payments(PAYEE, None))


def test_a_solana_payment_is_found_and_base58_case_is_kept(tmp_path):
    """A Solana send is recorded as ``solana_transfer``; reading only
    ``transfer`` answered "no record" for every Solana payee."""
    sol = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    path = _ledger(tmp_path, [_row(action="solana_transfer", counterparty=sol,
                                   chain="solana", result_ref="5sig")])
    rows = own_transfers_to(sol, path=path)
    assert [r["result_ref"] for r in rows] == ["5sig"]
    assert own_transfers_to(sol.lower(), path=path) == []


def test_an_nft_withdrawal_is_not_a_payment(tmp_path):
    path = _ledger(tmp_path, [_row(action="agent_nft_withdraw_token")])
    assert own_transfers_to(PAYEE, path=path) == []
