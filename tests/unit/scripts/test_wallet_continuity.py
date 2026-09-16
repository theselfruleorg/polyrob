"""scripts/wallet_continuity.py — the deploy must refuse to restart a
money-holding unit into a seedless or DIFFERENT wallet (2026-09-14)."""
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "wallet_continuity", Path(__file__).resolve().parents[3] / "scripts" / "wallet_continuity.py")
wc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wc)

EVM = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
SOL = "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL"


def _derive_ok(env, data_dir):
    return EVM, SOL


def _env(seed="s" * 64, enabled="true"):
    return {"AGENT_WALLET_ENABLED": enabled, "AGENT_WALLET_MASTER_SEED": seed}


def test_parse_env_file_quotes_comments_and_last_wins(tmp_path):
    f = tmp_path / "x.env"
    f.write_text('# c\nA="1"\nexport B=\'two\'\nA=3\nBAD\n')
    assert wc.parse_env_file(f) == {"A": "3", "B": "two"}
    assert wc.parse_env_file(tmp_path / "missing.env") == {}
    assert wc.parse_env_file(tmp_path) == {}   # a directory (e.g. /nonexistent on Ubuntu)


def test_disabled_wallet_is_not_guarded(tmp_path):
    problems, warnings = wc.check(_env(enabled="false"), tmp_path, derive=_derive_ok)
    assert problems == [] and warnings


def test_enabled_without_seed_refuses(tmp_path):
    problems, _ = wc.check(_env(seed=""), tmp_path, derive=_derive_ok)
    assert problems and "WITHOUT its wallet" in problems[0]


def test_no_record_only_warns(tmp_path):
    problems, warnings = wc.check(_env(), tmp_path, derive=_derive_ok)
    assert problems == []
    assert any("no address of record" in w for w in warnings)


def test_matching_public_identity_is_ok(tmp_path):
    (tmp_path / "wallet").mkdir()
    (tmp_path / "wallet" / "public_identity.json").write_text(
        json.dumps({"evm": {"treasury": EVM.lower()}, "solana": SOL}))
    problems, warnings = wc.check(_env(), tmp_path, derive=_derive_ok)
    assert problems == [] and warnings == []


def test_different_seed_refuses_with_both_addresses_named(tmp_path):
    (tmp_path / "wallet_balances.json").write_text(
        json.dumps({"address": "0x" + "1" * 40, "solana_address": SOL}))
    problems, _ = wc.check(_env(), tmp_path, derive=_derive_ok)
    assert len(problems) == 1
    assert "MISMATCH" in problems[0] and EVM in problems[0] and "0x" + "1" * 40 in problems[0]


def test_derivation_failure_is_a_refusal(tmp_path):
    def boom(env, data_dir):
        raise ValueError("corrupt meta.json")
    problems, _ = wc.check(_env(), tmp_path, derive=boom)
    assert problems and "corrupt meta.json" in problems[0]


def test_unit_not_loading_wallet_env_refuses(tmp_path):
    wenv = tmp_path / "wallet.env"
    wenv.write_text("AGENT_WALLET_MASTER_SEED=" + "s" * 64 + "\n")
    problems, _ = wc.check(_env(), tmp_path, wallet_envfile=wenv,
                           unit_envfiles=["/etc/polyrob/polyrob.env"], derive=_derive_ok)
    assert problems and "would restart seedless" in problems[0]
    problems, _ = wc.check(_env(), tmp_path, wallet_envfile=wenv,
                           unit_envfiles=["/etc/polyrob/polyrob.env", str(wenv)], derive=_derive_ok)
    assert problems == []


def test_main_exit_codes(tmp_path, monkeypatch):
    envf = tmp_path / "polyrob.env"; envf.write_text("AGENT_WALLET_ENABLED=true\n")
    wenv = tmp_path / "wallet.env"; wenv.write_text("")
    monkeypatch.setattr(wc, "derive_addresses", _derive_ok)
    rc = wc.main(["--envfile", str(envf), "--wallet-envfile", str(wenv), "--data-dir", str(tmp_path)])
    assert rc == 2  # enabled, no seed anywhere
    wenv.write_text("AGENT_WALLET_MASTER_SEED=" + "s" * 64 + "\n")
    rc = wc.main(["--envfile", str(envf), "--wallet-envfile", str(wenv), "--data-dir", str(tmp_path)])
    assert rc == 0
