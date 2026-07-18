"""M20 (2026-07-15 wallet/crypto security review): ``TreasurySweeper`` signs and
broadcasts real fund-moving transactions (``treasury_sweeper.py`` sweep loop) but
previously started on config presence alone — ``TREASURY_ADDRESS`` + a resolved
master seed, no enable flag, absent from the flag SSOT
(``core/initialization.py:680-693`` pre-fix).

This is a regression guard on the real code path (not just a flag default):
with a FULL treasury sweeper config (treasury address + wallet generator
available) but ``TREASURY_SWEEPER_ENABLED`` unset/false, ``initialize_auth_services``
must never construct/register/start a ``TreasurySweeper``. Only an explicit
``TREASURY_SWEEPER_ENABLED=true`` may.
"""
import asyncio

import pytest

from core.config import BotConfig
from core.initialization import _treasury_sweeper_enabled, initialize_auth_services


# ---------------------------------------------------------------------------
# Pure gate-parsing tests
# ---------------------------------------------------------------------------

def test_treasury_sweeper_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("TREASURY_SWEEPER_ENABLED", raising=False)
    assert _treasury_sweeper_enabled() is False


@pytest.mark.parametrize("falsey", ["false", "0", "off", "none", "no", ""])
def test_treasury_sweeper_enabled_falsey_set(monkeypatch, falsey):
    monkeypatch.setenv("TREASURY_SWEEPER_ENABLED", falsey)
    assert _treasury_sweeper_enabled() is False


def test_treasury_sweeper_enabled_explicit_true(monkeypatch):
    monkeypatch.setenv("TREASURY_SWEEPER_ENABLED", "true")
    assert _treasury_sweeper_enabled() is True


# ---------------------------------------------------------------------------
# Full-path regression: initialize_auth_services must honor the gate even
# with a completely valid, fund-moving-ready config.
# ---------------------------------------------------------------------------

class _StubDBManager:
    """Minimal stand-in — the auth-services init only reads `.tables`,
    `.user_profiles`, and `.connection` before doing any real DB work; the
    User-MCP-service tail is wrapped in its own try/except (optional)."""

    def __init__(self):
        self.tables = {}
        self.user_profiles = None
        self.connection = None


class _StubContainer:
    def __init__(self, config):
        self.config = config
        self.registered = {}
        self._services = {"database_manager": _StubDBManager(), "alchemy": None}

    def get_service(self, name):
        return self._services.get(name)

    def register_service(self, name, service):
        self.registered[name] = service


class _FakeTreasurySweeper:
    """Records construction + start() without touching real chains/signing."""

    instances = []

    def __init__(self, db_manager, wallet_generator, config):
        self.db_manager = db_manager
        self.wallet_generator = wallet_generator
        self.config = config
        self.started = False
        _FakeTreasurySweeper.instances.append(self)

    async def start(self):
        self.started = True


def _full_sweeper_config(monkeypatch, *, treasury_sweeper_env: str | None) -> BotConfig:
    """A config with everything the pre-fix code needed to start sweeping:
    ENABLE_AUTH on, a treasury address, and a resolvable master seed (so
    `wallet_generator` is non-None) — the exact 'full config' shape M20
    describes.
    """
    monkeypatch.setenv("ENABLE_AUTH", "true")
    monkeypatch.setenv("TREASURY_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("PAYMENT_MASTER_SEED", "s" * 40)
    monkeypatch.delenv("MASTER_SEED", raising=False)
    monkeypatch.setenv("ENABLE_CREDIT_SYSTEM", "false")
    monkeypatch.setenv("DEPOSIT_MONITOR_ENABLED", "false")
    monkeypatch.setenv("X402_ENABLED", "false")
    if treasury_sweeper_env is None:
        monkeypatch.delenv("TREASURY_SWEEPER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TREASURY_SWEEPER_ENABLED", treasury_sweeper_env)
    return BotConfig()


def test_full_config_flag_unset_sweeper_not_started(monkeypatch):
    """The core M20 regression: full fund-moving-ready config, flag unset ->
    TreasurySweeper must NOT be constructed, registered, or started."""
    _FakeTreasurySweeper.instances = []
    monkeypatch.setattr(
        "modules.payments.treasury_sweeper.TreasurySweeper", _FakeTreasurySweeper
    )

    config = _full_sweeper_config(monkeypatch, treasury_sweeper_env=None)
    assert config.enable_auth is True
    assert config.treasury_address

    container = _StubContainer(config)
    asyncio.run(initialize_auth_services(container))

    assert _FakeTreasurySweeper.instances == [], (
        "TreasurySweeper must not be constructed when TREASURY_SWEEPER_ENABLED "
        "is unset, even with a full treasury_address + wallet_generator config"
    )
    assert "treasury_sweeper" not in container.registered


def test_full_config_flag_false_sweeper_not_started(monkeypatch):
    """Same as above but with an explicit falsey value."""
    _FakeTreasurySweeper.instances = []
    monkeypatch.setattr(
        "modules.payments.treasury_sweeper.TreasurySweeper", _FakeTreasurySweeper
    )

    config = _full_sweeper_config(monkeypatch, treasury_sweeper_env="false")
    container = _StubContainer(config)
    asyncio.run(initialize_auth_services(container))

    assert _FakeTreasurySweeper.instances == []
    assert "treasury_sweeper" not in container.registered


def test_full_config_flag_true_sweeper_started(monkeypatch):
    """Flag explicitly true -> TreasurySweeper IS constructed, started, and
    registered on the container."""
    _FakeTreasurySweeper.instances = []
    monkeypatch.setattr(
        "modules.payments.treasury_sweeper.TreasurySweeper", _FakeTreasurySweeper
    )

    config = _full_sweeper_config(monkeypatch, treasury_sweeper_env="true")
    container = _StubContainer(config)
    asyncio.run(initialize_auth_services(container))

    assert len(_FakeTreasurySweeper.instances) == 1
    sweeper = _FakeTreasurySweeper.instances[0]
    assert sweeper.started is True
    assert container.registered.get("treasury_sweeper") is sweeper
