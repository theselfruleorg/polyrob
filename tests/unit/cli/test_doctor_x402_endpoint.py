"""§5.3 (self-contained x402 rail, 2026-08-21): `polyrob doctor` reports the
Tier-2 endpoint state and, when absent, the one-command runbook — so the
"need a mainnet-ready x402 endpoint" ask resolves to a documented owner step
instead of a blocked goal nobody can close."""
from cli.commands.doctor import doctor_report

RUNBOOK = "scripts/setup_x402_endpoint.sh"


def _joined(env):
    return "\n".join(doctor_report(env))


def test_endpoint_absent_names_the_runbook():
    out = _joined({})
    assert "x402 endpoint: not configured" in out
    assert RUNBOOK in out


def test_endpoint_live_shows_the_public_url():
    out = _joined({"X402_ENABLED": "true", "A2A_BASE_URL": "https://agent.example.com"})
    assert "x402 endpoint: https://agent.example.com" in out
    assert RUNBOOK not in out


def test_endpoint_enabled_without_base_url_is_flagged():
    out = _joined({"X402_ENABLED": "true"})
    assert "x402 endpoint: enabled but no A2A_BASE_URL" in out


def test_tier1_receive_is_never_reported_as_blocked_on_the_endpoint():
    """Tier 1 (invoice + on-chain detect) needs no endpoint at all — the row
    must not read as a blocker for getting paid."""
    out = _joined({"X402_INVOICE_ENABLED": "true",
                   "X402_PAYMENT_RECIPIENT": "0xEnvAddr"})
    assert "x402 endpoint: not configured" in out
    assert "invoices are unaffected" in out
