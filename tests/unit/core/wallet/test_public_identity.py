"""The wallet's PUBLIC half: a seedless process still knows WHO it is.

Prod splits ``AGENT_WALLET_MASTER_SEED`` into ``/etc/polyrob/wallet.env``, which
only the agent unit loads. The console and the email surface keep
``AGENT_WALLET_ENABLED=true`` and no seed, and every address-dependent READ
(portfolio, reconcile, the invoice treasury address, ``/api/webgate/positions``)
used to degrade to "no address to report holdings for".

Two halves are asserted here and they must never merge: an ADDRESS is public and
travels; SIGNING AUTHORITY does not, and asking for it must fail loudly rather
than quietly produce nothing.
"""
import json
import os
import stat

import pytest

import core.wallet.factory as factory
from core.wallet import public_identity
from core.wallet.agent_wallet import (AgentWallet, PublicOnlyWallet,
                                      WalletSigningUnavailable)
from core.wallet.config import load_wallet_config

SEED_A = "a" * 40
SEED_B = "b" * 40


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    """Every write in this file lands in tmp, never the developer's data home.

    ``POLYROB_DATA_DIR`` (NOT ``POLYROB_HOME``) is the lever: it is the one the
    wallet data dir resolves through, and the root conftest's wallet-audit-sink
    isolation defers to it at CALL time.
    """
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    # A leaked 'bip44' from an earlier CLI test would make these 40-char test
    # seeds die as "not a valid BIP-39 mnemonic" (see the sibling conftest).
    monkeypatch.delenv("AGENT_WALLET_DERIVATION", raising=False)
    return tmp_path / "home"


def _seeded_wallet(seed: str = SEED_A) -> AgentWallet:
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true",
                              "AGENT_WALLET_MASTER_SEED": seed})
    return AgentWallet(cfg)


# --- write / read ---------------------------------------------------------

def test_write_then_read_roundtrip(_isolated_data_home):
    wallet = _seeded_wallet()
    written = public_identity.write_public_identity(wallet)
    read_back = public_identity.read_public_identity()

    assert read_back == written
    assert set(read_back["evm"]) == {"treasury", "x402", "polymarket",
                                     "hyperliquid"}
    assert read_back["evm"]["treasury"] == wallet.signer_for("treasury").address
    assert read_back["evm"]["x402"] == wallet.signer_for("x402").address
    assert read_back["scheme"] == wallet.scheme
    assert read_back["operational_venue"] == wallet.operational_venue
    assert float(read_back["written_at"]) > 0


def test_record_carries_no_secret(_isolated_data_home):
    """It is PUBLIC data. Nothing derived-but-secret may ride along."""
    wallet = _seeded_wallet()
    public_identity.write_public_identity(wallet)
    body = open(public_identity.identity_path(), encoding="utf-8").read()
    assert SEED_A not in body
    assert "private" not in body.lower()
    assert "key" not in body.lower()


def test_file_is_world_readable(_isolated_data_home):
    """A file the console's own unit user cannot read is a file that does not
    do its job — and there is nothing secret in it to protect."""
    wallet = _seeded_wallet()
    public_identity.write_public_identity(wallet)
    mode = stat.S_IMODE(os.stat(public_identity.identity_path()).st_mode)
    assert mode == 0o644


def test_read_never_creates_the_file(_isolated_data_home):
    assert public_identity.read_public_identity() is None
    assert not os.path.exists(public_identity.identity_path())


def test_read_refuses_a_corrupt_record(_isolated_data_home):
    path = public_identity.identity_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("{not json")
    assert public_identity.read_public_identity() is None


def test_read_refuses_a_record_with_no_address(_isolated_data_home):
    path = public_identity.identity_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump({"evm": {}, "solana": None}, open(path, "w", encoding="utf-8"))
    assert public_identity.read_public_identity() is None


def test_solana_address_is_recorded(_isolated_data_home):
    pytest.importorskip("solders")
    wallet = _seeded_wallet()
    record = public_identity.write_public_identity(wallet)
    assert record["solana"] == wallet.solana_address


# --- publish (the seeded process's fail-open write) -----------------------

def test_maybe_publish_writes_once_then_skips(_isolated_data_home):
    wallet = _seeded_wallet()
    assert public_identity.maybe_publish(wallet) is True
    first = public_identity.read_public_identity()
    assert public_identity.maybe_publish(wallet) is False
    assert public_identity.read_public_identity() == first


def test_maybe_publish_rewrites_after_a_seed_change(_isolated_data_home):
    """A stale record is a money-visible lie — an owner funding an address the
    agent cannot spend from. The writer re-derives rather than trusting it."""
    public_identity.maybe_publish(_seeded_wallet(SEED_A))
    before = public_identity.read_public_identity()

    other = _seeded_wallet(SEED_B)
    assert public_identity.maybe_publish(other) is True
    after = public_identity.read_public_identity()
    assert after["evm"]["treasury"] != before["evm"]["treasury"]
    assert after["evm"]["treasury"] == other.signer_for("treasury").address


def test_maybe_publish_is_fail_open(_isolated_data_home, monkeypatch):
    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(public_identity, "write_public_identity", _boom)
    assert public_identity.maybe_publish(_seeded_wallet()) is False


# --- the public-only wallet ----------------------------------------------

def _public_wallet(record=None) -> PublicOnlyWallet:
    if record is None:
        record = public_identity.write_public_identity(_seeded_wallet())
        factory.reset_agent_wallet_cache()
    cfg = load_wallet_config({"AGENT_WALLET_ENABLED": "true"})
    return PublicOnlyWallet(cfg, record)


def test_public_only_wallet_knows_every_address(_isolated_data_home):
    seeded = _seeded_wallet()
    record = public_identity.write_public_identity(seeded)
    public = _public_wallet(record)

    assert public.address == seeded.address
    assert public.address_for_venue("treasury") == seeded.signer_for("treasury").address
    assert public.address_for_venue("x402") == seeded.signer_for("x402").address
    assert public.signer_for("treasury").address == seeded.signer_for("treasury").address
    assert public.operational_signer().address == seeded.address
    assert public.solana_address == seeded.solana_address
    assert public.signing_available is False
    assert seeded.signing_available is True


def test_seeded_wallet_also_answers_address_for_venue(_isolated_data_home):
    """The accessor is on the base class, so a caller never has to know which
    half of the split it is talking to."""
    seeded = _seeded_wallet()
    assert seeded.address_for_venue("treasury") == seeded.signer_for("treasury").address


def test_public_only_wallet_refuses_to_sign(_isolated_data_home):
    public = _public_wallet()
    signer = public.signer_for("treasury")

    with pytest.raises(WalletSigningUnavailable):
        signer.sign_transaction({"chainId": 8453})
    with pytest.raises(WalletSigningUnavailable):
        signer.sign_message(b"hello")
    with pytest.raises(WalletSigningUnavailable):
        signer.sign_typed_data({}, {}, {})
    with pytest.raises(WalletSigningUnavailable):
        signer.account
    with pytest.raises(WalletSigningUnavailable):
        public.account_for("hyperliquid")
    with pytest.raises(WalletSigningUnavailable):
        public._derive_key("treasury")


def test_refusal_says_where_the_seed_lives(_isolated_data_home):
    public = _public_wallet()
    with pytest.raises(WalletSigningUnavailable) as exc:
        public.signer_for("treasury").sign_transaction({"chainId": 8453})
    text = str(exc.value).lower()
    assert "no seed" in text
    assert "agent" in text


def test_public_only_solana_signer_refuses_to_sign(_isolated_data_home):
    pytest.importorskip("solders")
    public = _public_wallet()
    sol = public.solana_signer()
    assert sol.address == _seeded_wallet().solana_address
    with pytest.raises(WalletSigningUnavailable):
        sol.sign_transaction(object())
    with pytest.raises(WalletSigningUnavailable):
        sol.sign_transaction_with(object(), [])


def test_public_only_wallet_refuses_an_unrecorded_venue(_isolated_data_home):
    public = _public_wallet({"evm": {"treasury": "0x" + "11" * 20},
                             "scheme": "legacy"})
    assert public.address_for_venue("treasury") == "0x" + "11" * 20
    with pytest.raises(WalletSigningUnavailable):
        public.address_for_venue("hyperliquid")
    with pytest.raises(ValueError):
        public.address_for_venue("not-a-venue")


def test_public_only_address_never_names_a_delegated_key(_isolated_data_home):
    """`address` is the fund-me address. A record missing both spend venues
    must refuse by name rather than hand back a hyperliquid/polymarket key the
    agent cannot spend from — the fund-the-wrong-address footgun."""
    public = _public_wallet({"evm": {"hyperliquid": "0x" + "22" * 20},
                             "operational_venue": "treasury",
                             "scheme": "legacy"})
    with pytest.raises(WalletSigningUnavailable):
        public.address

    with_x402 = _public_wallet({"evm": {"hyperliquid": "0x" + "22" * 20,
                                        "x402": "0x" + "33" * 20},
                                "operational_venue": "treasury",
                                "scheme": "legacy"})
    assert with_x402.address == "0x" + "33" * 20


def test_public_only_wallet_still_has_a_policy_gate(_isolated_data_home):
    """The caps object is not signing authority — a read surface that renders
    the daily cap must keep working."""
    public = _public_wallet()
    assert public.policy is not None
    assert public.policy.daily_cap_usd == public.config.daily_cap_usd


# --- the factory's fallback order ----------------------------------------

def test_factory_falls_back_to_the_public_record(_isolated_data_home, monkeypatch):
    seeded = _seeded_wallet()
    public_identity.write_public_identity(seeded)

    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    wallet = factory.get_agent_wallet()
    assert isinstance(wallet, PublicOnlyWallet)
    assert wallet.address == seeded.address
    assert wallet.signing_available is False


def test_factory_still_fails_fast_with_no_seed_and_no_record(
        _isolated_data_home, monkeypatch):
    """A genuinely unconfigured deploy is unchanged: the loud ValueError the
    CLI turns into "run polyrob wallet init" still fires."""
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    with pytest.raises(ValueError, match="AGENT_WALLET_MASTER_SEED"):
        factory.get_agent_wallet()


def test_factory_publishes_the_record_when_seeded(_isolated_data_home, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED_A)
    factory.reset_agent_wallet_cache()

    wallet = factory.get_agent_wallet()
    assert not isinstance(wallet, PublicOnlyWallet)
    record = public_identity.read_public_identity()
    assert record is not None
    assert record["evm"]["treasury"] == wallet.signer_for("treasury").address


def test_factory_publish_failure_never_blocks_the_wallet(
        _isolated_data_home, monkeypatch):
    monkeypatch.setattr(public_identity, "write_public_identity",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED_A)
    factory.reset_agent_wallet_cache()

    wallet = factory.get_agent_wallet()
    assert wallet is not None and wallet.signing_available is True


def test_factory_disabled_wallet_is_unchanged(_isolated_data_home, monkeypatch):
    public_identity.write_public_identity(_seeded_wallet())
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    factory.reset_agent_wallet_cache()
    assert factory.get_agent_wallet() is None


def test_factory_public_only_mode_warns_once(_isolated_data_home, monkeypatch,
                                             caplog):
    public_identity.write_public_identity(_seeded_wallet())
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    with caplog.at_level("WARNING", logger="core.wallet.factory"):
        factory.get_agent_wallet()
        factory.get_agent_wallet()
    warnings = [r for r in caplog.records if "public-only" in r.getMessage().lower()]
    assert len(warnings) == 1


# --- the read surfaces the split exists for -------------------------------

def test_treasury_address_resolves_without_a_seed(_isolated_data_home, monkeypatch):
    """`resolve_treasury_address` is what an invoice/QR card is billed to."""
    seeded = _seeded_wallet()
    public_identity.write_public_identity(seeded)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    monkeypatch.delenv("X402_PAYMENT_RECIPIENT", raising=False)
    monkeypatch.setenv("X402_TREASURY_FROM_WALLET", "true")
    factory.reset_agent_wallet_cache()

    from modules.x402 import x402_integration
    assert (x402_integration.resolve_treasury_address()
            == seeded.signer_for("treasury").address)


def test_portfolio_holder_resolves_without_a_seed(_isolated_data_home, monkeypatch):
    """`defi_data.portfolio` said "agent wallet not enabled — no address to
    report holdings for" on the console box. It now has an address."""
    seeded = _seeded_wallet()
    public_identity.write_public_identity(seeded)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    from tools.defi.data_tool import DefiDataTool
    assert DefiDataTool()._resolve_holder() == seeded.address


def test_a_money_verb_returns_a_labeled_error_not_a_traceback(
        _isolated_data_home, monkeypatch):
    """The signing refusal must reach the agent as an ERROR STRING.

    `_run_guarded` already wraps the broadcast, so the refusal lands in the
    branch that says nothing was sent — which is the true statement.
    """
    public_identity.write_public_identity(_seeded_wallet())
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    factory.reset_agent_wallet_cache()

    wallet = factory.get_agent_wallet()
    signer = wallet.operational_signer()

    class _Rail:
        def size_gas(self, tx, used):
            return tx

        def sign_and_send(self, tx):
            return signer.sign_transaction(tx)   # the real refusal

    class _Decision:
        allowed, amount_usd, reason, lane = True, 1.0, "ok", "autonomous"
        sim_gas_used = 0

    from tools.defi.trade_tool import DefiTradeTool
    tool = DefiTradeTool(guard_fn=lambda *a, **k: _Decision())
    result = tool._run_guarded(
        intent=type("I", (), {"chain": "base"})(), tx={"chainId": 8453},
        rail=_Rail(), gate=wallet.policy, signer=signer,
        execution_context=None, header="", dry_run=False,
        venue_action="transfer", idem="t", counterparty="0x0")

    assert result.error and "broadcast failed" in result.error
    assert "no seed" in result.error.lower()
    assert "nothing was sent" in result.error
