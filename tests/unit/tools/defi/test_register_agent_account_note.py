"""115 W6 (B2) — ``register_agent`` says in its quote that a held NFT account already owns an
identity (it does not block: the instance wallet's entry is a second identity, linked by the
instance's registration file); an unreadable holdings file is named, never "none"."""
import pytest

from core.wallet import held_identity
from tests.unit.tools.defi.test_cr_m13_l15_erc8004_receipt import P, _env, _mint_log, _tool  # noqa: F401
from tools.defi import agent_registration as ar

ROW = {"chain": "robinhood", "chain_id": 4663, "collection": "0x" + "c0" * 20, "token_id": 3,
       "account": "0x" + "ac" * 20, "agent_id": 9, "registry": "0x" + "80" * 20}


def test_the_note_names_each_account_identity(monkeypatch):
    monkeypatch.setattr(held_identity, "account_identities", lambda: [ROW])
    note = ar.account_identity_note()
    assert "agentId 9 on robinhood" in note and ROW["account"] in note and "SECOND" in note


def test_no_account_identity_no_note_and_unreadable_is_named(monkeypatch):
    monkeypatch.setattr(held_identity, "account_identities", lambda: [])
    assert ar.account_identity_note() == ""

    def boom():
        raise ValueError("bad file")
    monkeypatch.setattr(held_identity, "account_identities", boom)
    assert "could not be read" in ar.account_identity_note()


@pytest.mark.asyncio
async def test_register_agent_quotes_the_note_and_still_runs(monkeypatch):
    monkeypatch.setattr(held_identity, "account_identities", lambda: [ROW])
    tool, gate, saved = _tool([_mint_log(7)], monkeypatch=monkeypatch)
    res = await tool.register_agent(P)
    assert res.error is None, res.error
    assert "an NFT account identity already exists: agentId 9" in res.extracted_content
