"""066 §5.5 + P3: the deposit seed, the sweeper and EIP-8004 signing move into
the signer under ``WALLET_SIGNER=remote`` — and nothing changes under local."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.core.signer.conftest import FakeRail, LoopbackClient


class _SweepRail(FakeRail):
    """FakeRail plus the balance/probe reads a sweep makes."""

    def _rpc(self, method, params, timeout=8.0):
        if method == "eth_getBalance":
            return hex(10 ** 16)
        if method == "eth_call":
            return hex(2_500_000)
        raise AssertionError(method)

    def _tx(self, to, value, data):
        return {"to": to, "value": value, "data": data, "nonce": 0,
                "gas": self.kw.get("gas_limit", 21_000), "maxFeePerGas": 10 ** 9,
                "maxPriorityFeePerGas": 10 ** 8, "chainId": 8453, "type": 2}

    def build_native_transfer(self, *, to, amount_wei):
        return self._tx(to, amount_wei, "0x")

    def build_erc20_transfer(self, *, token, to, amount_raw):
        data = "0xa9059cbb" + to[2:].lower().rjust(64, "0") + f"{amount_raw:064x}"
        return self._tx(token, 0, data)


@pytest.fixture
def sweep_rig(make_rig):
    from eth_utils import to_checksum_address
    from core.wallet import chains
    dest = to_checksum_address("0x" + "3" * 40)
    rig = make_rig(sweep={"destination": dest, "tokens": {"base:USDC": chains.get("base").usdc}})
    rig.service._rail_factory = _SweepRail
    rig.dest = dest
    return rig


def test_the_signer_sweeps_only_to_the_pinned_destination(sweep_rig):
    from modules.payments.wallet_generator import RemoteDepositWalletGenerator
    gen = RemoteDepositWalletGenerator(client=LoopbackClient(sweep_rig.service))
    addr = gen.generate_deposit_address("u-1")
    tx_hash = gen.sweep(user_id="u-1", chain="base", token_symbol="ETH", deposit_address=addr)
    sent = FakeRail.sent[-1]
    assert sent["hash"] == tx_hash and sent["from"] == addr
    assert sent["tx"]["to"] == sweep_rig.dest
    assert sent["tx"]["value"] == 10 ** 16 - 21_000 * 10 ** 9
    usdc = gen.sweep(user_id="u-2", chain="base", token_symbol="USDC",
                     deposit_address=gen.generate_deposit_address("u-2"))
    assert FakeRail.sent[-1]["hash"] == usdc
    assert sweep_rig.dest[2:].lower() in FakeRail.sent[-1]["tx"]["data"]
    # a sweep is not a treasury SPEND: the caps are not charged
    assert sweep_rig.service.gate.audit_log == []


def test_a_sweep_for_someone_elses_deposit_is_refused(sweep_rig):
    from core.signer.client import SignerRefused
    from modules.payments.wallet_generator import RemoteDepositWalletGenerator
    gen = RemoteDepositWalletGenerator(client=LoopbackClient(sweep_rig.service))
    with pytest.raises(SignerRefused):
        gen.sweep(user_id="u-1", chain="base", token_symbol="ETH",
                  deposit_address=gen.generate_deposit_address("u-2"))
    assert FakeRail.sent == []


def test_the_remote_generator_holds_no_key():
    from modules.payments.wallet_generator import RemoteDepositWalletGenerator
    gen = RemoteDepositWalletGenerator(client=object())
    with pytest.raises(ValueError):
        gen.get_private_key_for_user_id("u")
    with pytest.raises(ValueError):
        gen.get_account_for_sweep("u")


@pytest.mark.asyncio
async def test_the_sweeper_asks_the_signer_in_remote_mode(signer_home, monkeypatch):
    from modules.payments.treasury_sweeper import TreasurySweeper
    calls = []
    gen = SimpleNamespace(remote=True, sweep=lambda **kw: calls.append(kw) or "0x" + "ab" * 32)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)))
    sweeper = TreasurySweeper(db, gen, SimpleNamespace(
        treasury_address="0x" + "2" * 40, ethereum_rpc_url="https://example.invalid"))
    deposit = dict(id=1, user_id="u", chain="ethereum", user_address="0x" + "1" * 40,
                   deposit_address="0x" + "1" * 40, token_symbol="ETH", amount_usd=100)
    await sweeper._sweep_deposit(deposit)
    assert calls == [{"user_id": "u", "chain": "ethereum", "token_symbol": "ETH",
                      "deposit_address": "0x" + "1" * 40}]
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_eip8004_feedback_is_signed_by_the_signer_in_remote_mode(rig, monkeypatch):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    import core.signer.client as client_mod
    from modules.eip8004 import reputation
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    monkeypatch.delenv("EIP8004_AGENT_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(client_mod, "SignerClient", lambda *a, **k: LoopbackClient(rig.service))
    mgr = reputation.ReputationManager()
    mgr.config = SimpleNamespace(agent_id=7, agent_wallet="0x" + "c" * 40, chain_id=8453,
                                 reputation_registry_address=None)
    auth = await mgr.create_feedback_auth("0x" + "d" * 40, expires_in_seconds=3600)
    typed = mgr._build_typed_data(7, "0x" + "d" * 40, auth.expiresAt, auth.nonce)
    who = Account.recover_message(encode_typed_data(full_message=typed), signature=auth.signature)
    assert who == Account.from_key(rig.secrets["EIP8004_AGENT_PRIVATE_KEY"]).address
    assert auth.expiresAt > time.time()
