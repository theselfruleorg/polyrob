"""115 W6 (B2, E7) — which ERC-8004 identities this instance speaks for.

* the holdings watch records, for a held ERC-6551P NFT, the agentId its collection registered
  (read once) and whether the account still owns it (read each pass); an unreadable read
  changes nothing;
* ``held_identity.account_identities`` names only identities the account owns, and RAISES on an
  unreadable holdings file;
* ``feedback_target`` prefers an account identity, else the instance's own receipt record, and
  is None (never a guess) when there is neither or it cannot be read.
"""
import asyncio
import json

import pytest

from core.wallet import abi, erc8004, held_identity, nft_holdings
from tests.collection_pins import pin, polyrob_profile

PINNED = "0xC011000000000000000000000000000000000001"
TREASURY = "0x" + "2a" * 20
REGISTRY = erc8004.resolve_identity_registry("robinhood")
REPUTATION = erc8004.resolve_reputation_registry("robinhood")


def _w(v):
    return "0x" + "0" * 24 + v[2:].lower() if isinstance(v, str) else "0x" + format(int(v), "064x")


class Chain:
    def __init__(self, account):
        self.account = account
        self.has, self.agent_id, self.identity_owner = True, 9, account
        self.views_broken = self.owner_broken = False
        self.agent_reads = 0

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getLogs":
            return [{"address": PINNED, "blockNumber": "0x5", "logIndex": "0x0",
                     "topics": [nft_holdings.TOPIC_TRANSFER, _w("0x" + "00" * 20), _w(TREASURY),
                                _w(3)], "data": "0x"}]
        if method == "eth_call":
            to, sel = params[0]["to"].lower(), params[0]["data"][:10]
            if sel == abi.selector("ownerOf(uint256)"):
                if to == REGISTRY.lower():
                    if self.owner_broken:
                        raise RuntimeError("rpc down")
                    return _w(self.identity_owner)
                return _w(TREASURY)
            if sel in (abi.selector("hasIdentity(uint256)"), abi.selector("agentIdOf(uint256)")):
                if self.views_broken:
                    raise RuntimeError("rpc down")
                self.agent_reads += 1
                return _w(int(self.has) if sel == abi.selector("hasIdentity(uint256)") else self.agent_id)
            raise RuntimeError("execution reverted")
        raise AssertionError(f"{method} {params}")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    profile = polyrob_profile(PINNED, journal_prefix="POLYROB")
    pin(monkeypatch, profile)
    from core.wallet import collection_registry
    account, _v = collection_registry.profiles()[0].account_of(3)
    return tmp_path, account


def _tick(chain):
    async def notify(_c, _u, _t):
        return None
    return asyncio.run(nft_holdings.tick(None, treasury=TREASURY, rpc_for=lambda c: chain,
                                         notify=notify))


def test_the_watch_records_the_accounts_identity_and_reads_it_once(home):
    _home, account = home
    chain = Chain(account)
    _tick(chain)
    (row,) = nft_holdings.tracked()
    assert row["agent_id"] == 9 and row["identity_held"] is True
    reads = chain.agent_reads
    _tick(chain)
    assert chain.agent_reads == reads          # agentIdOf is read once
    (ident,) = held_identity.account_identities()
    assert ident == {"chain": "robinhood", "chain_id": 4663, "collection": PINNED.lower(),
                     "token_id": 3, "account": account, "agent_id": 9, "registry": REGISTRY}


def test_an_identity_moved_out_of_the_account_is_not_named(home):
    _home, account = home
    chain = Chain(account)
    _tick(chain)
    chain.identity_owner = "0x" + "77" * 20
    _tick(chain)
    assert nft_holdings.tracked()[0]["identity_held"] is False
    assert held_identity.account_identities() == []


def test_an_unreadable_identity_changes_nothing(home):
    _home, account = home
    chain = Chain(account)
    chain.views_broken = True
    _tick(chain)
    assert "agent_id" not in nft_holdings.tracked()[0]
    chain.views_broken = False
    _tick(chain)
    chain.owner_broken = True
    _tick(chain)                                # identity_held stays what it was
    assert nft_holdings.tracked()[0]["identity_held"] is True


def test_a_token_minted_without_an_identity_names_none(home):
    _home, account = home
    chain = Chain(account)
    chain.has = False
    _tick(chain)
    assert nft_holdings.tracked()[0]["agent_id"] is None
    assert held_identity.account_identities() == []


def test_an_unreadable_holdings_file_raises_and_feedback_says_nothing(home):
    _home, account = home
    _tick(Chain(account))
    path = nft_holdings.state_path()
    path.write_text("{not json")
    with pytest.raises(ValueError):
        held_identity.account_identities()
    assert held_identity.feedback_target() is None


def test_flag_off_names_no_account_identity(home, monkeypatch):
    _home, account = home
    _tick(Chain(account))
    monkeypatch.setenv("AGENT_NFT_ENABLED", "false")
    assert held_identity.account_identities() == []


def _own_record(home_dir, **over):
    from core.instance import erc8004_record_path, resolve_instance_id
    p = erc8004_record_path(home_dir, resolve_instance_id())
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"chain": "base", "chain_id": 8453, "registry": erc8004.resolve_identity_registry("base"),
           "agent_id": 42, "tx_hash": "0x" + "ab" * 32, "registered_at": "2026-11-12T00:00:00+00:00"}
    doc.update(over)
    p.write_text(json.dumps(doc))


def test_the_feedback_target_prefers_the_account_identity(home):
    home_dir, account = home
    _own_record(home_dir)
    _tick(Chain(account))
    target = held_identity.feedback_target()
    assert target["agent_id"] == 9 and target["reputation"] == REPUTATION
    line = held_identity.feedback_line(target)
    assert line == (f"To rate this agent on chain: giveFeedback for agentId 9 on the ERC-8004 "
                    f"Reputation Registry {REPUTATION} (robinhood, chain id 4663).")


def test_without_an_account_identity_the_instances_own_is_named(home):
    home_dir, _account = home
    _own_record(home_dir)
    target = held_identity.feedback_target()
    assert target["agent_id"] == 42 and target["chain"] == "base"
    assert target["reputation"] == erc8004.resolve_reputation_registry("base")


def test_a_record_of_an_unpinned_registry_or_nothing_names_no_target(home):
    home_dir, _account = home
    assert held_identity.feedback_target() is None and held_identity.feedback_line(None) == ""
    _own_record(home_dir, registry="0x" + "99" * 20)
    assert held_identity.feedback_target() is None
