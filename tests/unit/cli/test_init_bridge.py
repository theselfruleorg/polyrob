"""Task 7: onboarding bridge (inline key wizard -> full init), init next-steps
block, and the optional agent-wallet opt-in in the full ``polyrob init`` wizard.
"""
import os

import pytest
import click
from click.testing import CliRunner
from cli.commands import init as init_mod


def test_quick_setup_offers_bridge_and_declines(monkeypatch, tmp_path):
    monkeypatch.setattr(init_mod, "_prompt_provider_keys",
                        lambda collected: collected.update({"OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40}))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr("cli.keys._can_prompt", lambda: True)
    # run_quick_key_setup mutates os.environ directly — isolate (auto-restored).
    monkeypatch.setattr(os, "environ", dict(os.environ))
    prompts = []
    monkeypatch.setattr(click, "confirm", lambda msg, default=False: (prompts.append(msg), False)[1])
    ok = init_mod.run_quick_key_setup()
    assert ok
    assert any("full setup" in p.lower() for p in prompts)


def test_quick_setup_bridge_ctrl_c_is_cancelled_not_failed(monkeypatch, tmp_path, capsys):
    """Finding 4 regression (2026-07-14 final review): Ctrl-C/EOF inside the
    bridged ``polyrob init --skip-keys`` call must read as a cancellation, not
    a failure ("full setup failed (...)")."""
    monkeypatch.setattr(init_mod, "_prompt_provider_keys",
                        lambda collected: collected.update({"OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40}))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr("cli.keys._can_prompt", lambda: True)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(click, "confirm", lambda msg, default=False: True)

    def _raise_abort(*a, **k):
        raise click.Abort()
    monkeypatch.setattr(init_mod.init_cmd, "main", _raise_abort)

    ok = init_mod.run_quick_key_setup()

    assert ok  # a usable key was still collected — the bridge outcome is separate
    captured = capsys.readouterr()
    assert "cancelled" in captured.out.lower()
    assert "failed" not in captured.out.lower()


def test_quick_setup_bridge_not_offered_non_tty(monkeypatch, tmp_path):
    monkeypatch.setattr(init_mod, "_prompt_provider_keys",
                        lambda collected: collected.update({"OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40}))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr("cli.keys._can_prompt", lambda: False)
    # run_quick_key_setup mutates os.environ directly — isolate (auto-restored).
    monkeypatch.setattr(os, "environ", dict(os.environ))
    called = []
    monkeypatch.setattr(click, "confirm", lambda *a, **k: called.append(1) or False)
    ok = init_mod.run_quick_key_setup()
    assert ok
    assert not called          # no prompt without a TTY


def test_init_skip_keys_skips_section_one(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    runner = CliRunner()
    # --skip-keys + --quick: only the model prompt should appear.
    result = runner.invoke(init_mod.init_cmd, ["--skip-keys", "--quick"], input="\n")
    assert result.exit_code == 0, result.output
    assert "Section 1/6" not in result.output
    assert "Section 2/6" in result.output


def test_init_next_steps_block(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(init_mod.init_cmd, ["--quick", "--skip-keys"], input="\n")
    assert "Next steps" in result.output
    assert "wallet init" in result.output
    assert "pfp generate" in result.output


def test_init_wallet_optin_prompt_wired(monkeypatch, tmp_path):
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    called = {}
    monkeypatch.setattr("cli.commands.wallet.run_wallet_init_flow",
                        lambda **kw: called.update(kw) or
                        {"address": "0xabc", "scheme": "bip44",
                         "env_path": "", "linked_recipient": False})
    runner = CliRunner()
    # full wizard: blank everything, say yes ONLY to the wallet question.
    # Build the input by matching the CURRENT prompt order in init_cmd (keys
    # skipped via --skip-keys; the now-removed autonomy-budget prompt is NOT
    # in this list — see test_init_guardrails.py's _full_flow_input for the
    # reference sequence). One line per slot, in order:
    #   1. "" -> Section 2/6 default model            (blank = skip)
    #   2. "" -> Section 3/6 toolset                   (blank = default "default")
    #   3. "" -> Section 4/6 template                  (blank = default "general")
    #   4. "" -> Section 5/6 instance id               (blank = default "rob")
    #   5. "" -> Section 5/6 owner user id             (blank = default = instance id)
    #   6. "n" -> Section 6/6 enable local mode?        (No)
    #   7. "n" -> Section 6/6 apply approval preset?    (No)
    #   8. "" -> Section 6/6 daily digest channel       (blank = off)
    #   9. "y" -> Optional: agent crypto wallet opt-in  (YES <- what this test checks)
    result = runner.invoke(
        init_mod.init_cmd, ["--skip-keys"],
        input="\n\n\n\n\nn\nn\n\ny\n")
    assert result.exit_code == 0, result.output
    assert called, "wallet init flow was not invoked"
