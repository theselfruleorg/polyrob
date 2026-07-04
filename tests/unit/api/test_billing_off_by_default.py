"""C9: Posture 0/1 (own-ops, no explicit multitenant opt-in) must have NO
billing surface actually usable — own-ops uses the operator's own provider
API keys, never credits/x402.

**Correction to the original C9 brief** (recorded here so the discrepancy
isn't silently lost): the brief assumed ``BotConfig.enable_credit_system``
(``ENABLE_CREDIT_SYSTEM``) was the off-by-default gate and asserted it
defaults ``False``. It does not — it has defaulted ``True`` since
2025-11-13 (commit ``72cf279d``, long before Workstream C existed), so that
assertion would fail immediately, not "pass as a lock-in". Tracing the real
code path (``core/initialization.py::initialize_auth_services``, called
unconditionally from ``core/bot.py``) shows ``enable_credit_system`` is
inert by default: the function checks ``container.config.enable_auth``
FIRST and returns immediately when it is ``False`` — never reaching the
``enable_credit_system`` branch that would register
``balance_manager``/``tier_manager``/``api_key_manager``/
``wallet_generator``/the deposit monitor. So the actual own-ops billing
off-switch is ``ENABLE_AUTH`` (default ``False``), not
``ENABLE_CREDIT_SYSTEM``. This test locks the REAL gate instead of the
misidentified one, plus the already-correct x402/deposit-monitor defaults,
so Workstream B's ``POLYROB_POSTURE``/``webgate.is_multitenant()`` has
something concrete and accurate to wire onto.
"""
import asyncio

from core.config import BotConfig


def test_auth_system_defaults_off():
    """The real own-ops billing off-switch: ENABLE_AUTH defaults False, so
    initialize_auth_services() never registers any billing service (see
    core/initialization.py's early-return on `not container.config.enable_auth`).
    """
    config = BotConfig()
    assert config.enable_auth is False, (
        "own-ops (Posture 0/1) must never register billing services by "
        "default — enable_auth must default OFF"
    )


def test_auth_services_not_registered_when_auth_disabled():
    """Regression guard on the actual code path (not just the flag default):
    with enable_auth=False, initialize_auth_services() must return before
    registering balance_manager/tier_manager/api_key_manager/wallet_generator/
    the deposit monitor — i.e. no billing service ever reaches the container
    for an own-ops deployment that never opts into ENABLE_AUTH.
    """
    from core.initialization import initialize_auth_services

    class _StubContainer:
        """Minimal stand-in — initialize_auth_services only touches
        `.config` before its early-return when auth is disabled."""

        def __init__(self):
            self.config = BotConfig()  # enable_auth defaults False
            self.registered = []

        def register_service(self, name, service):  # pragma: no cover - must not be hit
            self.registered.append(name)

        def get_service(self, name):  # pragma: no cover - must not be hit
            return None

    container = _StubContainer()
    assert container.config.enable_auth is False

    asyncio.run(initialize_auth_services(container))

    assert container.registered == [], (
        "initialize_auth_services must not register any billing/auth "
        f"service when enable_auth=False; registered: {container.registered}"
    )


def test_x402_defaults_off(monkeypatch):
    monkeypatch.delenv("X402_ENABLED", raising=False)
    from modules.x402.x402_integration import get_x402_config
    assert get_x402_config()["enabled"] is False


def test_deposit_monitor_defaults_off():
    config = BotConfig()
    assert config.deposit_monitor_enabled is False
