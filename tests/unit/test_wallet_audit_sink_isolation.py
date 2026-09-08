"""Ratchet: no unit test may reach a REAL, on-disk wallet audit sink.

H3 fix round 3 (2026-08-22): ``core.wallet.factory.get_policy_gate()`` /
``get_agent_wallet()`` are process-level singletons built from
``core.wallet.audit_sink.default_audit_sink()``, which resolves
``<POLYROB_DATA_DIR or resolve_data_home()>/wallet/audit.jsonl`` — on a dev
machine running pytest from the repo root with no override, that is this
repo's own real ``.polyrob/wallet/audit.jsonl``. H3 gave ``WALLET_DAILY_CAP_USD``
a finite $100 default; the FIRST unit test in a session to reach
``get_policy_gate()`` silently built its singleton from a real, populated
audit trail (797 entries / $880 trailing-24h spend, diagnosed live on
2026-08-22) and kept that cached object for the rest of the session, failing
FOUR tests outside the wallet-owned test directories with "daily spend cap
$100.00 would be exceeded" — a hazard that had been LATENT (silently reading
real data, no assertion tripped) until H3's finite default turned it into hard
failures.

The leak ran in BOTH directions. Reading was the visible half; writing was the
silent half — the real file carried unit-test-authored rows
(``place_market_order`` $10/$10/$100 against venue ``hyperliquid``) written by
the suite itself, so the suite was inflating the very trailing-24h number it
then failed on. This ratchet covers both directions.

The root-level fix is ``tests/conftest.py::_isolate_wallet_audit_sink``
(autouse, every test in the suite). It monkeypatches the ONE resolver shared by
the audit sink AND ``core.wallet.derivation``
(``core.wallet.audit_sink._wallet_data_dir``) rather than the global
``POLYROB_DATA_DIR`` env var — an earlier attempt that set the env var globally
broke seven UNRELATED tests that depend on it being unset (CWD-based data-home
resolution, CLI container path injection, profile/prefs write locations).

This file makes the wallet-specific guarantee ASSERTABLE — it fails loudly if
the isolation ever regresses, instead of silently reading (and appending to)
real spend history again the next time someone adds a money test outside the
directories that happen to have their own local guard.
"""
import os

import pytest


def test_wallet_audit_sink_resolves_under_the_tests_own_tmp_path(tmp_path):
    """``_wallet_data_dir()`` — the SAME resolver ``default_audit_sink()`` (and
    ``core.wallet.derivation``'s meta.json) use — must resolve under THIS
    test's own ``tmp_path``, never the developer's real data home. Runs with
    zero manual setup, relying only on the root-level autouse isolation every
    other unit test also gets — if that fixture ever stops applying, THIS test
    fails instead of some unrelated money test reading real spend history
    again."""
    from core.wallet.audit_sink import _wallet_data_dir
    resolved = _wallet_data_dir()
    assert resolved.startswith(str(tmp_path)), (
        f"wallet audit sink resolved to {resolved!r}, which is NOT under this "
        f"test's own tmp_path ({str(tmp_path)!r}) — "
        "tests/conftest.py::_isolate_wallet_audit_sink failed to redirect "
        "core.wallet.audit_sink._wallet_data_dir, so a unit test can reach a "
        "REAL, on-disk audit sink (this is the H3 fix-round-3 regression: 4 "
        "tests failed against 797 real entries / $880 of real trailing-24h "
        "spend, and the suite was appending its own rows to that same file)."
    )


def test_wallet_audit_sink_honors_an_explicit_data_dir():
    """Escape hatch 1: an explicit ``data_dir`` argument (a test that wants to
    exercise a SPECIFIC location) must still reach the real resolver unchanged
    — the isolation only substitutes the no-override default, it must never
    silently redirect a caller who passed their own path."""
    from core.wallet.audit_sink import _wallet_data_dir
    resolved = _wallet_data_dir("/tmp/some-explicit-dir")
    assert resolved == os.path.join("/tmp/some-explicit-dir", "wallet")


def test_wallet_audit_sink_honors_a_test_set_data_dir_env(tmp_path, monkeypatch):
    """Escape hatch 2: a test that sets ``POLYROB_DATA_DIR`` itself must still
    get the REAL env-anchored resolution (absolutized per L3), not the
    fixture's substitute. Without this, the wallet's own regression tests
    (``test_wallet_meta_resolution_survives_cwd_change_via_data_dir_env``,
    ``test_wallet_meta_path_and_audit_sink_share_directory_by_default``) would
    be silently NEUTERED — they would assert the fixture's behaviour instead of
    the production resolver's. This does not reopen the leak: the leak path is
    the no-override branch, and ``_isolate_path_manager`` pops the var before
    every test, so any value seen here was set by the test itself."""
    home = tmp_path / "explicit-home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    from core.wallet.audit_sink import _wallet_data_dir
    assert _wallet_data_dir() == os.path.join(str(home.resolve()), "wallet")


def test_get_policy_gate_never_sees_real_trailing_spend():
    """End-to-end regression test for the READ half of the failure: a freshly
    built ``PolicyGate``, via the REAL process-level factory singleton every
    tool calls (``tools/hyperliquid/service.py``, ``tools/polymarket/service.py``,
    ...), must start with an EMPTY audit log in a unit test — never a
    developer's real spend history. This is the exact call path the four
    originally-failing tests went through."""
    from core.wallet.factory import get_policy_gate, reset_agent_wallet_cache
    reset_agent_wallet_cache()
    try:
        gate = get_policy_gate()
        assert gate.audit_log == [], (
            f"a freshly built PolicyGate saw {len(gate.audit_log)} pre-existing "
            "audit entries in a unit test — it reached a REAL durable audit "
            "sink instead of an isolated one. If this fires, "
            "tests/conftest.py::_isolate_wallet_audit_sink has regressed."
        )
        # The specific decision that broke: a small, ordinary spend must be
        # allowed against a clean gate — never refused by leftover real
        # trailing-24h spend from the developer's own wallet usage.
        decision = gate.check(venue="hyperliquid", amount_usd=5.0,
                              idempotency_key="ratchet-probe")
        assert decision.allowed, f"a $5 spend against a fresh gate was refused: {decision.reason}"
    finally:
        reset_agent_wallet_cache()


def test_get_policy_gate_write_lands_in_the_isolated_sink_not_the_real_one(tmp_path):
    """The WRITE half: recording a spend through the real factory singleton
    must land under the isolated tmp root, and the real
    ``resolve_data_home()/wallet/audit.jsonl`` must be byte-for-byte untouched
    by running the unit suite.

    ⚠️ The isolation is asserted BEFORE anything is recorded, deliberately: an
    earlier version of this ratchet recorded first, so when the isolation was
    absent it appended its own ``$1.00 ratchet-write-probe`` row to the
    developer's REAL money audit log (visible in that file today). A guard must
    not commit the very act it exists to forbid. The real path is computed via
    the real, UNPATCHED ``resolve_data_home()`` (only ``_wallet_data_dir`` is
    monkeypatched), so this holds on any machine."""
    from core.runtime_paths import resolve_data_home
    from core.wallet.audit_sink import _wallet_data_dir
    from core.wallet.factory import get_policy_gate, reset_agent_wallet_cache

    isolated_dir = _wallet_data_dir()
    if not isolated_dir.startswith(str(tmp_path)):
        pytest.fail(
            f"the wallet data dir resolved to {isolated_dir!r}, not under this "
            f"test's tmp_path ({str(tmp_path)!r}) — REFUSING to record a probe "
            "spend, because doing so would append a fake row to the "
            "developer's real money audit log. "
            "tests/conftest.py::_isolate_wallet_audit_sink has regressed."
        )

    real_audit_path = str(resolve_data_home() / "wallet" / "audit.jsonl")
    real_size_before = (os.path.getsize(real_audit_path)
                        if os.path.exists(real_audit_path) else None)

    reset_agent_wallet_cache()
    try:
        gate = get_policy_gate()
        gate.record(venue="hyperliquid", action="ratchet-write-probe", amount_usd=1.0,
                    counterparty=None, idempotency_key="ratchet-write-probe",
                    result_ref=None)
        isolated_audit_path = os.path.join(isolated_dir, "audit.jsonl")
        # The sink is JSONL-backed whenever it could be constructed; if it fell
        # back to an in-memory list there is nothing on disk to check, which is
        # equally safe — either way the REAL path below must be untouched.
        if os.path.exists(isolated_audit_path):
            assert "ratchet-write-probe" in open(isolated_audit_path, encoding="utf-8").read(), (
                "the probe spend did not land in the isolated sink"
            )
        real_size_after = (os.path.getsize(real_audit_path)
                           if os.path.exists(real_audit_path) else None)
        assert real_size_after == real_size_before, (
            f"the real {real_audit_path} grew ({real_size_before} -> "
            f"{real_size_after} bytes) — a unit test WROTE to the developer's "
            "real durable audit sink"
        )
    finally:
        reset_agent_wallet_cache()
