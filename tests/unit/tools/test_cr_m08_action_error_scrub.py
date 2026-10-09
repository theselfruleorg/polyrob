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


# Prod 2026-10-04 15:24 and 17:00: the file tool's record guard refused a
# rewrite with a ServiceError. The policy message was all the agent needed, but
# the full traceback rode into ActionResult.error and so into every later
# prompt. A ServiceError cause reaches the agent as its message only; the log
# keeps the traceback.

def test_a_service_error_reaches_the_agent_without_a_traceback(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    from core.exceptions import ServiceError
    from tools.controller.execution import _agent_facing_error
    try:
        try:
            raise ServiceError("Refusing to rewrite reports/x.md: it is a RECORD")
        except ServiceError as inner:
            raise RuntimeError(f"Error executing action filesystem_write_file: {inner}") from inner
    except RuntimeError as e:
        out = _agent_facing_error("filesystem_write_file", e, "Traceback (most recent call last):\n  ...")
    assert "Refusing to rewrite reports/x.md" in out
    assert "Traceback" not in out


def test_other_errors_keep_their_traceback_for_the_agent(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    from tools.controller.execution import _agent_facing_error
    out = _agent_facing_error("x", RuntimeError("boom"), "tb")
    assert out == "Error executing action x: boom\ntb"
