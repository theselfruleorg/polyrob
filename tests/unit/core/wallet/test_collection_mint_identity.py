"""115 W7 queued fix — the mint's ERC-8004 identity (polyrob-desk
``contracts/src/kit/modules/AgentIdentity.sol:27-28, :50-89``): each minted id's account
registers an identity at mint — ``IdentityRegistered(id, agentId)`` — or the mint goes on
without one, ``IdentitySkipped(id)``, and nothing registers it later. The guard refuses a mint
that skips a declared id, and a collection that emits identity events must register EVERY
declared id. A collection without the module emits neither and is not held to it."""
import pytest

from core.wallet import collection_mint as M
from tests.collection_pins import pin
from tests.unit.core.wallet.test_tx_guard_collection_mint import (
    COLLECTION, IDS, _authorize, _deltas, _intent, _log, _mint_logs, _word)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    pin(monkeypatch, COLLECTION)


def test_the_identity_topics_are_their_signatures():
    from eth_utils import keccak
    assert M.TOPIC_IDENTITY_REGISTERED == "0x" + keccak(
        text="IdentityRegistered(uint256,uint256)").hex()
    assert M.TOPIC_IDENTITY_SKIPPED == "0x" + keccak(text="IdentitySkipped(uint256)").hex()


def _with(*extra):
    return _deltas(logs=_mint_logs() + tuple(extra))


def _registered(i, agent=100):
    return _log(M.TOPIC_IDENTITY_REGISTERED, _word(i), _word(agent + i))


def test_every_declared_id_registered_is_allowed():
    d = _authorize(_intent(), _with(*(_registered(i) for i in IDS)), price=100.0)
    assert d.allowed is True, d.reason


def test_a_skipped_identity_refuses_the_mint():
    d = _authorize(_intent(), _with(_registered(5), _registered(6),
                                    _log(M.TOPIC_IDENTITY_SKIPPED, _word(7))), price=100.0)
    assert d.allowed is False and "WITHOUT an ERC-8004 identity" in d.reason and "[7]" in d.reason


def test_a_declared_id_with_neither_event_refuses_when_the_collection_registers():
    d = _authorize(_intent(), _with(_registered(5), _registered(6)), price=100.0)
    assert d.allowed is False and "no IdentityRegistered for ids [7]" in d.reason, d.reason


def test_an_identity_event_from_another_contract_does_not_count():
    other = _log(M.TOPIC_IDENTITY_REGISTERED, _word(7), _word(1),
                 address="0x" + "99" * 20)
    d = _authorize(_intent(), _with(_registered(5), _registered(6), other), price=100.0)
    assert d.allowed is False and "[7]" in d.reason


def test_a_collection_without_the_identity_module_is_not_held_to_it():
    d = _authorize(_intent(), _with(), price=100.0)
    assert d.allowed is True, d.reason
