"""M17 (2026-07-15): `polyrob init` must not promise invoicing that stays OFF.

The wallet can PAY x402 paywalls immediately, but INVOICING/getting-paid needs
X402_INVOICE_ENABLED (default OFF, previously named in zero CLI hints). When the
owner opts into the wallet at init, the flag must be named so the promise isn't
broken.
"""
from click.testing import CliRunner

import cli.commands.init as init_mod


def test_init_wallet_optin_names_invoice_flag(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr("cli.commands.wallet.run_wallet_init_flow",
                        lambda **kw: {"address": "0xabc", "scheme": "bip44",
                                      "env_path": "", "linked_recipient": False})
    runner = CliRunner()
    # Full wizard, keys skipped; answer YES only to the wallet question (same input
    # shape as test_init_bridge.test_init_wallet_optin_prompt_wired — see that
    # test for the per-line prompt-order comment; the now-removed autonomy-budget
    # prompt is NOT one of these 9 slots):
    #   1. "" model  2. "" toolset  3. "" template  4. "" instance id
    #   5. "" owner id  6. "n" local mode?  7. "n" approval preset?
    #   8. "" digest channel  9. "y" wallet opt-in (<- what this test checks)
    result = runner.invoke(
        init_mod.init_cmd, ["--skip-keys"],
        input="\n\n\n\n\nn\nn\n\ny\n")
    assert result.exit_code == 0, result.output
    assert "X402_INVOICE_ENABLED" in result.output


def test_init_wallet_prompt_does_not_overpromise(monkeypatch, tmp_path):
    """The opt-in prompt no longer claims unconditional 'can invoice, get paid'
    — it scopes invoicing to a further flag."""
    import inspect
    # init_cmd is a click.Command — the source lives on its callback.
    src = inspect.getsource(init_mod.init_cmd.callback)
    # The old unconditional promise string must be gone.
    assert "can invoice, get paid, and pay" not in src
