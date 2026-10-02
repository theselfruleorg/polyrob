"""067 P1b: the kernel ledger and the hooks a rail registers."""
import asyncio

import pytest

import core.money.hooks as hooks


def test_policy_gate_is_the_kernel_ledger():
    from core.money.ledger import SpendLedger
    from core.wallet.policy import PolicyGate
    assert PolicyGate is SpendLedger


def test_the_wallet_registers_the_cap_resolver(monkeypatch):
    """load_wallet_config builds its resolver through the hook; the default
    provider is the wallet's own live_caps_resolver."""
    import core.wallet.config as cfg
    seen = []

    def fake(env=None, *, user_id=None, home_dir=None):
        seen.append(user_id)
        return lambda: (1.0, 2.0)
    monkeypatch.setattr(cfg, "live_caps_resolver", fake)
    resolver = hooks.cap_resolver({}, user_id="u1", home_dir=None)
    assert resolver() == (1.0, 2.0) and seen == ["u1"]


def test_spend_ledger_getter_is_late_bound(monkeypatch):
    import core.wallet.factory as factory
    sentinel = object()
    monkeypatch.setattr(factory, "get_policy_gate", lambda: sentinel)
    assert hooks.get_spend_ledger() is sentinel


def test_a_registration_replaces_the_provider(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    marker = object()
    hooks.register_spend_ledger(lambda: marker)
    assert hooks.get_spend_ledger() is marker


def test_no_registration_means_unknown(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    assert not hasattr(hooks, "_DEFAULT_MODULES")      # P5a: no fallback import
    assert hooks.get_spend_ledger() is None
    assert hooks.submission_journal() is None
    assert hooks.position_book() is None
    assert hooks.cap_resolver() is None
    assert asyncio.run(hooks.treasury_balance_usd()) is None


def test_a_raising_treasury_probe_is_unknown(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})

    async def boom():
        raise RuntimeError("rpc down")
    hooks.register_treasury_balance(boom)
    assert asyncio.run(hooks.treasury_balance_usd()) is None


def test_register_refuses_a_non_callable():
    with pytest.raises(TypeError):
        hooks.register_spend_ledger("nope")


def test_the_ledger_module_imports_nothing_above_core():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4] / "core" / "money"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                assert top not in {"modules", "agents", "tools", "api", "cli",
                                   "surfaces", "webview", "cron", "packs"}, (path.name, name)


# --- 067 P5a: the journal + position book are hooks ------------------------- #

import subprocess
import sys
import textwrap

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[4]


def _fresh(code: str) -> str:
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_the_wallet_package_import_registers_every_kind():
    """ONE place registers; importing the package (no chain deps) is enough."""
    assert _fresh("""
        import core.money.hooks as h
        before = h.registered()
        import core.wallet
        print(before, h.registered())
    """) == ("() ('cap_resolver', 'position_book', 'spend_ledger', "
             "'submission_journal', 'treasury_balance')")


def test_phase_one_registers_the_in_core_rails_at_process_entry():
    """Every entry (CLI main, API app, console, conftest) runs phase 1 first."""
    assert _fresh("""
        import core.money.hooks as h
        from core.packs.loader import register_policies
        register_policies()
        print(len(h.registered()))
    """) == "5"


def test_the_kernel_alone_refuses_without_a_journal():
    """No rail registered = the interlock is unavailable = every spend refused
    (the same sentence as the old failed import)."""
    assert _fresh("""
        from core.config_policy import AutonomyConfig
        AutonomyConfig.autonomy_halted = staticmethod(lambda: False)
        from core.money.ledger import SpendLedger
        d = SpendLedger(max_per_tx_usd=10).check(venue="v", amount_usd=1, idempotency_key=None)
        print(d.allowed, d.reason)
    """) == "False wallet submission journal unavailable; spending refused"


class _Journal:
    def __init__(self, unresolved=()):
        self._u = list(unresolved)
        self.booked = []

    def unresolved(self):
        return self._u

    def mark_booked(self, ref, *, amount_usd=None, venue=None):
        self.booked.append((ref, amount_usd, venue))


class _HealthySink(list):
    healthy = True


def _ledger(**kw):
    from core.money.ledger import SpendLedger
    return SpendLedger(max_per_tx_usd=100, **kw)


def test_check_reads_the_registered_journal(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    j = _Journal(unresolved=["0xabc"])
    hooks.register_submission_journal(lambda: j)
    d = _ledger().check(venue="v", amount_usd=1, idempotency_key=None)
    assert (d.allowed, d.reason) == (
        False, "unaccounted wallet submission; reconcile before further spending")
    j._u = []
    assert _ledger().check(venue="v", amount_usd=1, idempotency_key=None).allowed is True


def test_a_raising_journal_refuses(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})

    def boom():
        raise ImportError("no chain deps")
    hooks.register_submission_journal(boom)
    d = _ledger().check(venue="v", amount_usd=1, idempotency_key=None)
    assert (d.allowed, d.reason) == (False, "wallet submission journal unavailable; spending refused")


def test_record_releases_the_interlock_through_the_hook(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    j = _Journal()
    hooks.register_submission_journal(lambda: j)
    _ledger(audit_sink=_HealthySink()).record(
        venue="v", action="swap", amount_usd=2.0, counterparty=None,
        idempotency_key="k", result_ref="0xtx", submission_ref="sub1")
    assert j.booked == [("0xtx", 2.0, "v"), ("sub1", 2.0, "v")]


def test_record_on_a_durable_sink_without_a_journal_raises(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    with pytest.raises(RuntimeError):
        _ledger(audit_sink=_HealthySink()).record(
            venue="v", action="swap", amount_usd=2.0, counterparty=None,
            idempotency_key=None, result_ref="0xtx")


def test_record_writes_positions_through_the_book_hook(monkeypatch):
    from core.exec_identity import reset_exec_identity, set_exec_identity
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    seen = []
    hooks.register_position_book(lambda uid, deltas: seen.append((uid, deltas)))
    token = set_exec_identity("u9", "s1")
    try:
        _ledger().record(venue="v", action="swap", amount_usd=1.0, counterparty=None,
                         idempotency_key=None, result_ref="0x1", positions=["d1"])
    finally:
        reset_exec_identity(token)
    assert seen == [("u9", ["d1"])]


def test_no_book_skips_the_position_write(monkeypatch):
    monkeypatch.setattr(hooks, "_PROVIDERS", {})
    _ledger().record(venue="v", action="swap", amount_usd=1.0, counterparty=None,
                     idempotency_key=None, result_ref="0x1", positions=["d1"])


def test_the_kernel_imports_no_rail_module():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4] / "core" / "money"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("core.wallet", "core.open_positions",
                                            "core.signer")), (path.name, name)
