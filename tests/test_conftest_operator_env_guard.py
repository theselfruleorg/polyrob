"""Contract test for the conftest operator-env guard (2026-08-28).

``tests/conftest.py::_restore_operator_env_vars`` restores a NAMED set of env
vars after every test, so one test's ``load_env`` — which raw-writes the dev
box operator's real ``~/.polyrob/.env`` into ``os.environ`` — cannot poison
later tests. The set being hand-maintained has now missed a var twice:

- 2026-08-14: ``ZAI_API_KEY``, fixed by DERIVING the provider keys from the
  spec registry (``_provider_key_env_vars``) instead of hand-listing them.
- 2026-08-27: ``AGENT_WALLET_DERIVATION``. A CLI test's ``load_env`` injected
  ``AGENT_WALLET_DERIVATION=bip44``; it survived to the end of the session, so
  every later test that derived a key from a TEST seed raised "not a valid
  BIP-39 mnemonic but derivation is 'bip44'" — 21 failures in
  ``tools/hyperliquid`` and ``tools/x402`` that all passed in isolation.

The money-rail block is the dangerous one: a leaked wallet var does not just
fail tests, it can point wallet code at the operator's REAL scheme/address.
So the wallet set is derived from the flags catalog (the env-flag SSOT, which
a reverse contract test already forces every new flag into) rather than
hand-listed, and this test asserts the derivation actually covers it.
"""
import os

from tests.conftest import _OPERATOR_ENV_VARS


def _catalogued_wallet_flags() -> list:
    from core.flags_catalog import CATALOG
    return [name for name, _group, _default, _desc in CATALOG
            if name.startswith("AGENT_WALLET_") and "<" not in name]


def test_catalog_actually_lists_wallet_flags():
    """Guard the guard: if the catalog stopped carrying wallet rows, the
    derivation below would silently cover nothing."""
    assert len(_catalogued_wallet_flags()) >= 5


def test_every_catalogued_wallet_flag_is_guarded():
    """Every AGENT_WALLET_* flag the catalog knows must be restored after each
    test. A missing one is the 2026-08-27 failure mode."""
    missing = sorted(set(_catalogued_wallet_flags()) - set(_OPERATOR_ENV_VARS))
    assert not missing, (
        f"wallet env vars not covered by the conftest restore guard: {missing}. "
        "A leaked wallet var poisons every later test in the session and can "
        "point wallet code at the operator's real derivation scheme."
    )


def test_guard_restores_a_raw_write():
    """The mechanism, not just the list: an UNMANAGED os.environ write of a
    guarded var must be undone by the restore fixture. Simulates exactly what
    load_env does.

    The fixture is driven directly rather than relied on around this test —
    a test-function finalizer runs BEFORE the autouse fixture's teardown, so
    an in-test assertion could never observe the restore.
    """
    from tests.conftest import _restore_operator_env_vars

    var = "AGENT_WALLET_DERIVATION"
    assert var in _OPERATOR_ENV_VARS
    before = os.environ.get(var)

    gen = _restore_operator_env_vars.__wrapped__()
    next(gen)  # setup: snapshot the guarded vars
    os.environ[var] = "bip44-sentinel"
    try:
        next(gen)  # teardown: restore
    except StopIteration:
        pass

    assert os.environ.get(var) == before, (
        f"the restore fixture did not undo a raw write of {var} "
        f"(got {os.environ.get(var)!r}, expected {before!r})"
    )
