"""E6 — PAYMENT_MASTER_SEED must resolve to the SAME value everywhere a wallet
generator is built, so the container path (api/payment_endpoints.py) and the
webview startup path never derive different deposit addresses for the same
user_id. The legacy MASTER_SEED alias was retired in 1.4: alone, it refuses.
"""
import pytest

from core.config_policy.retired_flags import RetiredFlagError
from core.payment_config import resolve_master_seed


def test_prefers_payment_master_seed(monkeypatch):
    monkeypatch.setenv("PAYMENT_MASTER_SEED", "a" * 40)
    monkeypatch.setenv("MASTER_SEED", "b" * 40)
    assert resolve_master_seed() == "a" * 40


def test_retired_master_seed_alone_refuses(monkeypatch):
    monkeypatch.delenv("PAYMENT_MASTER_SEED", raising=False)
    monkeypatch.setenv("MASTER_SEED", "c" * 40)
    with pytest.raises(RetiredFlagError, match="PAYMENT_MASTER_SEED") as exc:
        resolve_master_seed()
    assert "c" * 40 not in str(exc.value)  # never echo the seed


def test_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("PAYMENT_MASTER_SEED", raising=False)
    monkeypatch.delenv("MASTER_SEED", raising=False)
    assert resolve_master_seed() is None


def test_same_seed_yields_the_same_deposit_address(monkeypatch):
    """Regression for the split-brain bug: both call sites now resolve through
    the same function, so DepositWalletGenerator produces the SAME address for
    a given user_id regardless of which process path built it."""
    monkeypatch.setenv("PAYMENT_MASTER_SEED", "d" * 40)
    monkeypatch.delenv("MASTER_SEED", raising=False)

    from modules.payments.wallet_generator import DepositWalletGenerator

    seed_a = resolve_master_seed()  # what polyrob/app/initialization.py now uses
    seed_b = resolve_master_seed()  # what webview/server.py now uses
    assert seed_a == seed_b
    gen_a = DepositWalletGenerator(seed_a)
    gen_b = DepositWalletGenerator(seed_b)
    assert gen_a.generate_deposit_address("user-1") == gen_b.generate_deposit_address("user-1")


def test_wallet_generator_never_logs_the_seed(caplog):
    import logging
    from modules.payments.wallet_generator import DepositWalletGenerator

    caplog.set_level(logging.DEBUG)
    secret = "s3cr3t-master-seed-value-01234567"
    gen = DepositWalletGenerator(secret)
    gen.generate_deposit_address("user-1")
    for record in caplog.records:
        assert secret not in record.getMessage()
