"""W1.1 tail (2026-08-21): `polyrob doctor` reports the x402 treasury and its
SOURCE (env | agent wallet auto | none + remedy), pure over the env dict —
so an operator can see where invoice money will land without reading code."""
from cli.commands.doctor import doctor_report

SEED = "s" * 40


def _joined(env):
    return "\n".join(doctor_report(env))


def test_treasury_explicit_env_named_as_source():
    out = _joined({"X402_PAYMENT_RECIPIENT": "0xEnvAddr"})
    assert "x402 treasury: 0xEnvAddr (env X402_PAYMENT_RECIPIENT)" in out


def test_treasury_wallet_auto_when_env_empty_and_wallet_on():
    out = _joined({"AGENT_WALLET_ENABLED": "true",
                   "AGENT_WALLET_MASTER_SEED": SEED})
    assert "x402 treasury: agent wallet address (auto" in out


def test_treasury_wallet_auto_suppressed_by_flag_off():
    out = _joined({"AGENT_WALLET_ENABLED": "true",
                   "AGENT_WALLET_MASTER_SEED": SEED,
                   "X402_TREASURY_FROM_WALLET": "false"})
    assert "x402 treasury: none" in out


def test_treasury_none_names_the_remedy():
    out = _joined({})
    assert "x402 treasury: none" in out
    assert "X402_PAYMENT_RECIPIENT" in out


def test_blank_flag_reads_as_the_default_like_bool_env():
    """A5 (2026-08-24 audit): doctor parses X402_TREASURY_FROM_WALLET exactly as
    core.env.bool_env does. A present-but-blank value is the DEFAULT (on) — the
    old hand-rolled tuple treated blank as false, so doctor reported "none"
    while invoicing resolved the wallet address."""
    out = _joined({"AGENT_WALLET_ENABLED": "true",
                   "AGENT_WALLET_MASTER_SEED": SEED,
                   "X402_TREASURY_FROM_WALLET": ""})
    assert "x402 treasury: agent wallet address (auto" in out
