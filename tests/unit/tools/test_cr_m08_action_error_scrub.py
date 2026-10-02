"""CR-M08: a traceback that carries the loaded wallet seed never reaches ActionResult.error."""
from tools.controller.execution import _dedup_action_error

SEED = ("legal winner thank year wave sausage worth useful legal winner "
        "thank yellow")


def test_action_error_redacts_loaded_seed(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    err = ValueError(f"Provided words: '{SEED}'")
    out = _dedup_action_error("defi_trade_swap", err, f"Traceback ...\nValueError: {SEED}\n")
    assert SEED not in out
    assert "wave sausage worth useful" not in out
    assert out.startswith("Error executing action defi_trade_swap")


def test_action_error_without_secret_is_unchanged(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    out = _dedup_action_error("x", RuntimeError("boom"), "tb")
    assert out == "Error executing action x: boom\ntb"
