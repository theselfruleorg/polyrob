"""113 W6 G-5: owner reads for the NFT account and the signing history.

`/nft account [<id>] on <chain>` is the agent's own ``agent_nft_snapshot`` read
from the owner's seat; `/wallet signed [n]` reads the one signing journal
(``tools/wallet_sign/store.py``). Both send and sign nothing; a missing journal
is not created, and an unreadable one is not "nothing signed".
"""
import asyncio

from surfaces.telegram import nft_ops, signing_ops


def test_nft_account_runs_the_snapshot_read(monkeypatch):
    seen = {}

    async def snapshot(self, params, ctx):
        seen.update(chain=params.chain, nft=params.nft)
        return type("R", (), {"error": None, "extracted_content": "ACCOUNT VIEW"})()

    monkeypatch.setattr("tools.agent_nft.tool.AgentNftTool.agent_nft_snapshot", snapshot)
    out = asyncio.run(nft_ops.nft_reply("u_owner", ["account", "7", "on", "robinhood"]))
    assert out == "ACCOUNT VIEW"
    assert seen == {"chain": "robinhood", "nft": "7"}


def test_nft_account_needs_the_chain():
    out = asyncio.run(nft_ops.nft_reply("u_owner", ["account"]))
    assert "/nft account … on <network>" in out


def test_signing_history_without_a_journal_creates_none(monkeypatch, tmp_path):
    path = tmp_path / "wallet" / "wallet_sign.db"
    monkeypatch.setattr("tools.wallet_sign.store.store_path", lambda data_home=None: str(path))
    out = signing_ops.signing_history_reply([])
    assert "signed nothing yet" in out
    assert not path.exists()


def test_signing_history_lists_the_journal(monkeypatch, tmp_path):
    from tools.wallet_sign.store import WalletSignStore
    path = tmp_path / "wallet" / "wallet_sign.db"
    store = WalletSignStore(str(path))
    store.reserve(scheme="eip191", origin="example.test", kind="login", cls="A",
                  digest="d1", address="0xabc", autonomous=True, nonce="n1",
                  now=1_700_000_000.0, per_origin_day=5, all_origins_day=10)
    monkeypatch.setattr("tools.wallet_sign.store.store_path", lambda data_home=None: str(path))
    out = signing_ops.signing_history_reply(["5"])
    assert "example.test — login (eip191, class A, by an autonomous run)" in out
    assert "d1" not in out  # the digest is not shown


def test_an_unreadable_journal_is_not_nothing_signed(monkeypatch, tmp_path):
    path = tmp_path / "wallet_sign.db"
    path.write_text("not a database")
    monkeypatch.setattr("tools.wallet_sign.store.store_path", lambda data_home=None: str(path))
    out = signing_ops.signing_history_reply([])
    assert "could not read the signing journal" in out and "not 'nothing signed'" in out


def test_wallet_signed_is_the_owner_verb(monkeypatch):
    monkeypatch.setattr("core.wallet.authority.owner_refusal", lambda uid: None)
    monkeypatch.setattr("surfaces.telegram.signing_ops.signing_history_reply",
                        lambda args: f"HISTORY {args}")
    from surfaces.telegram.owner_ops import wallet_reply
    assert wallet_reply(["signed", "3"], user_id="u_owner") == "HISTORY ['3']"
