"""026 P0.2/P0.3 — `doctor --flags` frozen-flag honesty + autonomy-mode header.

The gap (G4): the report printed `AGENT_COMPUTE_POSTURE = 2 [env]` while the
runtime ran the import-frozen 0 — the single most security-critical flag was
the one the report lied about. The report now prints the FROZEN value as
effective and marks a differing env value INERT. The clamp state
(`autonomy_mode_display`) gets a header line in `--flags` too (it was only in
plain `doctor`, and the two views are mutually exclusive).
"""
import pytest

from cli.commands.doctor import flags_report


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AUTONOMY_MODE", "AUTONOMY_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL",
                "AGENT_COMPUTE_POSTURE", "PAYMENT_APPROVAL_MODE"):
        monkeypatch.delenv(var, raising=False)


def test_frozen_posture_env_value_reported_inert(monkeypatch):
    # The pytest process froze AGENT_COMPUTE_POSTURE=0 at import; a later env
    # value must never be reported as effective.
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    lines = flags_report({"AGENT_COMPUTE_POSTURE": "2"})
    row = next(ln for ln in lines if "AGENT_COMPUTE_POSTURE" in ln)
    assert "= 0" in row
    assert "frozen at import" in row
    assert "INERT" in row
    assert "env value 2" in row


def test_frozen_payment_mode_env_value_reported_inert(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    lines = flags_report({"PAYMENT_APPROVAL_MODE": "auto"})
    row = next(ln for ln in lines if "PAYMENT_APPROVAL_MODE" in ln)
    assert "= approve" in row
    assert "INERT" in row


def test_frozen_flag_agreeing_env_value_stays_normal():
    # env value equal to the frozen value -> plain [env] line, no INERT noise.
    lines = flags_report({"AGENT_COMPUTE_POSTURE": "0"})
    row = next(ln for ln in lines if "AGENT_COMPUTE_POSTURE" in ln)
    assert "INERT" not in row
    assert "[env]" in row


def test_flags_report_has_autonomy_mode_header():
    lines = flags_report({})
    header = next(ln for ln in lines if ln.startswith("autonomy mode:"))
    assert "supervised" in header


def test_flags_report_header_shows_clamp(monkeypatch):
    # autonomous requested, no owner bound -> the header must say clamped.
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    from core.config_policy import policy
    policy.reset_autonomy_mode_warnings()
    try:
        lines = flags_report({"AUTONOMY_MODE": "autonomous", "POLYROB_LOCAL": "1"})
    finally:
        policy.reset_autonomy_mode_warnings()
    header = next(ln for ln in lines if ln.startswith("autonomy mode:"))
    assert "clamped" in header
