import re
import core.wallet.factory as f

HEX64 = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")


def test_no_action_surface_returns_private_key(monkeypatch, request):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    f.reset_agent_wallet_cache()
    # Ensure cache is always cleared after the test so downstream tests that
    # construct X402PayTool(wallet=None) don't accidentally inherit this wallet.
    request.addfinalizer(f.reset_agent_wallet_cache)
    w = f.get_agent_wallet()
    # The raw derived key for a venue:
    raw = w._derive_key("x402").hex()
    # Public surfaces must never contain it:
    surfaces = [w.address, repr(w.signer_for("x402")), str(w.signer_for("x402").address)]
    for s in surfaces:
        assert raw not in s.lower()
