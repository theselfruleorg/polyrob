"""`polyrob wallet init` with no --data-dir pins meta.json where the runtime reads it.

The admin path passed the DATA HOME as the wallet dir, so the write-once scheme
landed at <home>/meta.json while `derivation.resolve_scheme` reads
<home>/wallet/meta.json — a bip44 wallet could later resolve as legacy.
"""
import json
import os

import pytest
from click.testing import CliRunner

from cli.commands.wallet import wallet_cmd


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for k in ("AGENT_WALLET_MASTER_SEED", "AGENT_WALLET_ENABLED",
              "AGENT_WALLET_DERIVATION", "X402_PAYMENT_RECIPIENT"):
        monkeypatch.delenv(k, raising=False)
    yield
    for k in ("AGENT_WALLET_MASTER_SEED", "AGENT_WALLET_ENABLED",
              "AGENT_WALLET_DERIVATION", "X402_PAYMENT_RECIPIENT"):
        os.environ.pop(k, None)


def test_init_without_data_dir_writes_meta_under_wallet_dir(tmp_path, monkeypatch):
    pytest.importorskip("eth_account")
    home = tmp_path / "datahome"
    home.mkdir()
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setattr("cli._admin_home.admin_data_dir", lambda **k: str(home))
    result = CliRunner().invoke(wallet_cmd, ["init", "--yes", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (home / "meta.json").exists()
    meta = json.loads((home / "wallet" / "meta.json").read_text())
    assert meta["derivation"] == "bip44"

    from core.wallet import derivation
    assert derivation.wallet_meta_path(home / "wallet") == home / "wallet" / "meta.json"
