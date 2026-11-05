"""A /send proposal that repeats a CONFIRMED send of the same token to the same
address (inside 72 h) is refused unless the agent says another payment is owed
(prod 2026-11-04: a re-proposed round batch carried four winners already paid)."""
import asyncio
import time

from core.surfaces import cards
from tests.unit.tools.controller.test_card_actions import _actions, _ctx, _owner  # noqa: F401

TOKEN = "0x1111111111111111111111111111111111111111"
A = "0x7abe582a59412f278f14dcfca098c0fde11a1ca3"
B = "0x765256dD0eFE7c8CEE8fa796f3636A6DC1bFc7C1"


def _propose(cmd, **kw):
    fn, model, _ = _actions()["propose_action"]
    return asyncio.run(fn(model(command=cmd, why="round", **kw), execution_context=_ctx()))


def _paid(to, age_s=60.0, result="SENT AND CONFIRMED tx 0xabc"):
    card = cards._new("alice", cards.KIND_QUOTE, "test", "Quote", verb="/send",
                      args=["311628", TOKEN, "to", to, "on", "robinhood"], ttl=3600.0)
    done = cards.store().transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_DONE,
                                    result=result)
    assert done is not None
    if age_s:
        conn = cards.store()._conn()
        try:
            conn.execute("UPDATE cards SET updated_at=? WHERE card_id=?",
                         (time.time() - age_s, card.card_id))
            conn.commit()
        finally:
            conn.close()


def test_repeat_of_a_confirmed_send_is_refused(_owner):  # noqa: F811
    _paid(A)
    res = _propose(f"/send 311628 {TOKEN} to {A.upper().replace('0X', '0x')} on robinhood")
    assert res.error and "already paid" in res.error


def test_also_owed_allows_it(_owner):  # noqa: F811
    _paid(A)
    assert _propose(f"/send 311628 {TOKEN} to {A} on robinhood", also_owed=True).error is None


def test_other_address_old_send_and_failed_send_do_not_block(_owner):  # noqa: F811
    _paid(B)
    _paid(A, age_s=80 * 3600.0)
    assert _propose(f"/send 311628 {TOKEN} to {A} on robinhood").error is None
    _paid("0x1111111111111111111111111111111111111111", result="failed: reverted")
    assert _propose(f"/send 311628 {TOKEN} to 0x1111111111111111111111111111111111111111 on robinhood").error is None


def test_a_gas_symbol_repeats_native_and_another_chain_does_not(_owner):  # noqa: F811
    from tools.controller.card_actions import _send_asset
    assert _send_asset(["1", "eth", "to", A, "on", "ethereum"]) == ("ethereum", "native")
    assert _send_asset(["1", "native", "to", A, "on", "ethereum"]) == ("ethereum", "native")
    assert _send_asset(["1", "native", "to", A, "on", "base"]) != (
        _send_asset(["1", "native", "to", A, "on", "ethereum"]))


def test_a_send_that_runs_now_counts_as_paid(_owner):  # noqa: F811
    card = cards._new("alice", cards.KIND_QUOTE, "test", "Quote", verb="/send",
                      args=["311628", TOKEN, "to", A, "on", "robinhood"], ttl=3600.0)
    assert cards.store().transition(card.card_id, frozenset({cards.S_OPEN}), cards.S_CONFIRMED)
    assert "already paid" in (_propose(f"/send 311628 {TOKEN} to {A} on robinhood").error or "")
