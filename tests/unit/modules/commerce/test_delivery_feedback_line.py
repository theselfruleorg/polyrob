"""115 W6 (E7) — the buyer's completion message names where to leave feedback ON CHAIN: the
ERC-8004 Reputation Registry, its chain and the agentId of the identity this agent speaks for
(a held NFT account's, else its own). Only when an identity is recorded; never a signature;
never on a public thread (a post stays short)."""
import pytest

from core.commerce import orders as ords
from core.wallet import held_identity
from modules.commerce import service
from tests.unit.modules.commerce.test_109_delivery_proof import TX, _rpc, good, order  # noqa: F401

REP = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
TARGET = {"chain": "robinhood", "chain_id": 4663, "agent_id": 9, "reputation": REP,
          "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432", "account": "0x" + "ac" * 20}


def _capture(monkeypatch, target):
    sent = []

    async def _tell(container, o, text, media=None):
        sent.append(text)
        return True
    monkeypatch.setattr(service, "tell_buyer", _tell)
    monkeypatch.setattr(service, "_rpc_call", _rpc(good()))
    monkeypatch.setattr(held_identity, "feedback_target", lambda *a, **k: target)
    return sent


@pytest.mark.asyncio
async def test_the_completion_message_names_the_on_chain_feedback(order, monkeypatch):  # noqa: F811
    sent = _capture(monkeypatch, TARGET)
    await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    assert sent[0].endswith(f"To rate this agent on chain: giveFeedback for agentId 9 on the "
                            f"ERC-8004 Reputation Registry {REP} (robinhood, chain id 4663).")
    assert "sign" not in sent[0].lower()


@pytest.mark.asyncio
async def test_no_identity_no_line(order, monkeypatch):  # noqa: F811
    sent = _capture(monkeypatch, None)
    await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    assert "giveFeedback" not in sent[0] and "delivered" in sent[0]


@pytest.mark.asyncio
async def test_a_public_thread_gets_no_feedback_line(order, monkeypatch):  # noqa: F811
    from core.commerce import store as st
    from core.sqlite_util import execute_retry
    execute_retry(st.db_path(), "UPDATE offer_orders SET thread_ref='101', channel='x' "
                                "WHERE id=?", (order.id,))
    sent = _capture(monkeypatch, TARGET)

    async def _owner(container, uid, text):
        return None
    monkeypatch.setattr(service, "owner_notice", _owner)
    await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    assert "giveFeedback" not in sent[0]
    assert ords.get(order.id).status in (ords.DELIVERED, ords.ACCEPTED)
