"""J1 — a journal entry with NO action, and the absence of an adopt verb (proposal 115 W1).

* ``tool.write_journal_entry``: with a pinned ``journal_log`` the entry goes ON CHAIN as the
  account's single ``JournalLog.log`` call (``is_journal_entry``); without one, to the local file.
* There is no ``agent_nft_adopt``: holding the NFT is the whole relation. The holdings watch
  records what an arrived account already holds (``tests/unit/core/wallet/test_nft_inherited.py``).
"""
import os
import types

import pytest

from core.wallet import erc6551, journal_log, nft_account
from core.wallet.signer import LocalEoaSigner
from tests.collection_pins import pin, profile
from tests.unit.tools.test_agent_nft_withdraw import (ACCOUNT, PINNED, Chain, FakeRail, _run,
                                                      _tool)
from tools.agent_nft.tool import JournalParams

JLOG = "0x10910910910910910910910910910910910910a1"


class JournalChain(Chain):
    """``Chain`` plus a JournalLog history (``entries``) — or a broken one."""

    def __init__(self, owner, entries=(), broken=False):
        super().__init__(owner)
        self.entries, self.broken = list(entries), broken

    def __call__(self, method, params, *a, **k):
        if method == "eth_getLogs" and str(params[0].get("address", "")).lower() == JLOG.lower():
            if self.broken:
                raise RuntimeError("getLogs refused")
            out = []
            for i, e in enumerate(self.entries):
                data = journal_log.encode_log(nft_account.canonical(e))
                out.append({"address": JLOG, "blockNumber": hex(0x20 + i), "logIndex": "0x0",
                            "topics": [journal_log.TOPIC_ENTRY,
                                       "0x" + "0" * 24 + ACCOUNT.lower()[2:]],
                            "data": "0x" + data[len(journal_log.LOG_SELECTOR):]})
            return out
        return super().__call__(method, params, *a, **k)


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, dict(profile(PINNED, journal_prefix="POLYROB"), journal_log=JLOG))
    FakeRail.sent = []
    return LocalEoaSigner(os.urandom(32))


@pytest.fixture
def owner_turn(monkeypatch):
    import core.security.owner_turn as ot
    monkeypatch.setattr(ot, "owner_turn_refusal", lambda *a, **k: None)


def _seller_chain():
    seller = LocalEoaSigner(os.urandom(32))
    e0 = nft_account.sign_entry(nft_account.build_entry(
        prior=[], account=ACCOUNT, chain_id=4663, kind="thesis", text="TKN", owner=seller.address),
        seller, prefix="POLYROB")
    return seller, [e0]


def _entry_of(tx):
    to, value, data, op = erc6551.decode_execute(tx["data"])
    assert (to.lower(), value, op) == (JLOG.lower(), 0, 0)
    return journal_log.parse_entry(journal_log.decode_log(data))


# --- a journal entry with no action --------------------------------------------------------

def test_a_note_goes_on_chain_as_the_accounts_single_call(armed):
    tool, seen = _tool(armed, JournalChain(armed.address))
    res = _run(tool.write_journal_entry(JournalParams(chain="robinhood", kind="note", text="hi", nft="3", dry_run=False),
                                        kind="note", text="hi"))
    assert res.error is None, res.error
    intent, tx, _ = seen[0]
    assert intent.is_journal_entry and intent.via_account == ACCOUNT and intent.to == JLOG
    assert intent.amount_raw == 0 and intent.token is None
    entry = _entry_of(tx)
    assert entry["kind"] == "note" and entry["seq"] == 0
    assert nft_account.recover_owner(entry, "POLYROB") == armed.address.lower()
    held = nft_account.HeldNft("robinhood", 4663, PINNED.lower(), 3, ACCOUNT, "POLYROB", JLOG.lower())
    assert nft_account.load_journal(nft_account._held_path(held)) == [entry]   # cached


def test_without_a_journal_log_the_note_is_local(armed, monkeypatch):
    pin(monkeypatch, profile(PINNED, journal_prefix="POLYROB"))
    tool, seen = _tool(armed, Chain(armed.address))
    res = _run(tool.write_journal_entry(JournalParams(chain="robinhood", kind="note", text="hi", nft="3", dry_run=False),
                                        kind="note", text="hi"))
    assert seen == [] and "local journal" in res.extracted_content
    assert "journal: entry #0 (note) signed" in res.extracted_content


# --- no adopt verb ---------------------------------------------------------------------------

def test_there_is_no_adopt_verb():
    """Holding the NFT is the whole relation (proposal 115): no ceremony gives the account."""
    from tools.agent_nft.tool import AgentNftTool
    assert not hasattr(AgentNftTool, "agent_nft_adopt")
    from core.verb_policy_rows import CORE_VERB_ROWS
    assert "agent_nft_adopt" not in str(CORE_VERB_ROWS)
