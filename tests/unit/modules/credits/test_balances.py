import threading

import pytest
from modules.credits import balances as B


@pytest.mark.asyncio
async def test_provider_balance_none_when_not_openrouter(monkeypatch):
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "anthropic")
    assert await B.provider_balance_usd() is None


@pytest.mark.asyncio
async def test_provider_balance_openrouter_computes_remaining(monkeypatch):
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    async def fake_get(url, headers=None, timeout=None):
        return {"data": {"total_credits": 90.0, "total_usage": 90.165018562}}
    monkeypatch.setattr(B, "_get_json", fake_get)

    bal = await B.provider_balance_usd()
    # Display-only USD figure rounds to 6 decimals — the file's convention
    # (unified_ledger.py rounds every money field to 6). abs tolerance avoids
    # hand-computing the exact 6th-decimal rounding of the raw float.
    assert bal == pytest.approx(-0.165018562, abs=1e-6)


@pytest.mark.asyncio
async def test_provider_balance_none_on_error(monkeypatch):
    """Fail-open: an errored probe is None (unknown), NEVER 0.0 (a lie)."""
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    async def boom(url, headers=None, timeout=None):
        raise RuntimeError("network down")
    monkeypatch.setattr(B, "_get_json", boom)

    assert await B.provider_balance_usd() is None


@pytest.mark.asyncio
async def test_provider_balance_chat_provider_takes_precedence(monkeypatch):
    """CHAT_PROVIDER ('which provider is actually active' — task_agent_lite.py,
    cli/config_store.py) must win over a stale DEFAULT_PROVIDER. If
    DEFAULT_PROVIDER won here, this would wrongly return None (anthropic isn't
    openrouter) even though the deploy is actually pinned to openrouter."""
    monkeypatch.setenv("CHAT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    async def fake_get(url, headers=None, timeout=None):
        return {"data": {"total_credits": 10.0, "total_usage": 1.0}}
    monkeypatch.setattr(B, "_get_json", fake_get)

    assert await B.provider_balance_usd() == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_ledger_include_balances_false_never_probes(monkeypatch):
    """The default must perform NO network read — recap calls this via a sync bridge."""
    from modules.credits.unified_ledger import build_ledger
    from tests.unit.modules.credits.test_unified_ledger_split import FakeDB

    called = []

    async def spy():
        called.append(1)
        return 5.0
    monkeypatch.setattr(B, "provider_balance_usd", spy)

    led = await build_ledger("rob", days=1, db=FakeDB())
    assert called == []
    assert led["runtime"]["provider_balance_usd"] is None


# --- treasury_balance_usd -------------------------------------------------

class _FakeWallet:
    def __init__(self, address):
        self.address = address


@pytest.mark.asyncio
async def test_treasury_balance_returns_usdc_figure(monkeypatch):
    """balances() returns a 2-tuple (native, usdc), NOT a dict — a dict access
    would be a bug. Confirm the tuple is unpacked correctly and usdc (index 1)
    is what's surfaced."""
    import core.wallet.factory as factory_mod
    import core.wallet.onchain as onchain_mod

    monkeypatch.setattr(factory_mod, "get_agent_wallet", lambda: _FakeWallet("0xabc"))
    monkeypatch.setattr(onchain_mod, "balances", lambda addr, chain, timeout=4.0: (0.5, 12.34))

    assert await B.treasury_balance_usd("rob") == 12.34


@pytest.mark.asyncio
async def test_treasury_balance_none_when_wallet_missing(monkeypatch):
    import core.wallet.factory as factory_mod

    monkeypatch.setattr(factory_mod, "get_agent_wallet", lambda: None)
    assert await B.treasury_balance_usd("rob") is None


@pytest.mark.asyncio
async def test_treasury_balance_none_when_address_missing(monkeypatch):
    import core.wallet.factory as factory_mod

    monkeypatch.setattr(factory_mod, "get_agent_wallet", lambda: _FakeWallet(None))
    assert await B.treasury_balance_usd("rob") is None


@pytest.mark.asyncio
async def test_treasury_balance_none_when_probe_returns_none_none(monkeypatch):
    """balances() returns (None, None) on any on-chain read failure — never a
    fabricated $0.00 (H14b)."""
    import core.wallet.factory as factory_mod
    import core.wallet.onchain as onchain_mod

    monkeypatch.setattr(factory_mod, "get_agent_wallet", lambda: _FakeWallet("0xabc"))
    monkeypatch.setattr(onchain_mod, "balances", lambda addr, chain, timeout=4.0: (None, None))

    assert await B.treasury_balance_usd("rob") is None


@pytest.mark.asyncio
async def test_treasury_balance_none_when_probe_raises(monkeypatch):
    import core.wallet.factory as factory_mod

    def boom():
        raise RuntimeError("wallet factory exploded")
    monkeypatch.setattr(factory_mod, "get_agent_wallet", boom)

    assert await B.treasury_balance_usd("rob") is None


@pytest.mark.asyncio
async def test_treasury_balance_runs_onchain_call_off_event_loop(monkeypatch):
    """core/wallet/onchain.py::balances() is a BLOCKING urllib call (two RPC
    round-trips x 4s timeout). async def alone does not make it non-blocking —
    this must be dispatched off the event loop (e.g. via asyncio.to_thread),
    or a later live-loop caller stalls for up to ~8s per probe."""
    import core.wallet.factory as factory_mod
    import core.wallet.onchain as onchain_mod

    main_thread = threading.current_thread()
    seen_threads = []

    def fake_balances(addr, chain, timeout=4.0):
        seen_threads.append(threading.current_thread())
        return (0.1, 5.0)

    monkeypatch.setattr(factory_mod, "get_agent_wallet", lambda: _FakeWallet("0xabc"))
    monkeypatch.setattr(onchain_mod, "balances", fake_balances)

    bal = await B.treasury_balance_usd("rob")
    assert bal == 5.0
    assert seen_threads, "onchain_balances was never called"
    assert seen_threads[0] is not main_thread, (
        "blocking onchain_balances() ran on the event-loop thread instead of "
        "being dispatched off-loop (e.g. asyncio.to_thread)"
    )


# --- build_ledger(include_balances=True) ----------------------------------

@pytest.mark.asyncio
async def test_build_ledger_include_balances_true_populates_both_fields(monkeypatch):
    """The opt-in path actually wires both probes into the ledger output —
    not just proven correct by inspection."""
    from modules.credits.unified_ledger import build_ledger
    from tests.unit.modules.credits.test_unified_ledger_split import FakeDB

    async def fake_treasury(user_id):
        return 12.34

    async def fake_provider():
        return 5.67

    monkeypatch.setattr(B, "treasury_balance_usd", fake_treasury)
    monkeypatch.setattr(B, "provider_balance_usd", fake_provider)

    led = await build_ledger("rob", days=1, db=FakeDB(), include_balances=True)
    assert led["treasury"]["balance_usd"] == 12.34
    assert led["runtime"]["provider_balance_usd"] == 5.67
