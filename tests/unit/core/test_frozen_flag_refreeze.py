"""026 P1.1 — the import-frozen policy flags must see the env-file ladder.

Failure mode A: ``polyrob/app/bootstrap.py`` imports ``core.config_policy`` at module
scope, so AGENT_COMPUTE_POSTURE and the payment-approval flags froze BEFORE
any entrypoint ran ``load_env`` — writing them to any ``.polyrob/.env`` was a
permanent no-op on every CLI path. ``load_env`` now re-freezes them EXACTLY
ONCE per process, right after file layering, before any container/agent
exists. The security property is preserved: a mid-session ``os.environ``
mutation still cannot move them (the once-guard has been consumed), and the
values still come only from owner-controlled files/process env at start.
"""
import os

import pytest

import polyrob.app.bootstrap as bootstrap
from core.config_policy import policy


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    for var in ("AGENT_COMPUTE_POSTURE", "PAYMENT_APPROVAL_MODE",
                "APPROVAL_TIMEOUT_SEC", "APPROVAL_GRANT_TTL_HOURS",
                "CONFIG_ENV", "ENV"):
        monkeypatch.delenv(var, raising=False)
    # Isolate from the developer's real home ladder + project files.
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "_POLICY_REFROZEN", False)
    yield
    # Realign the frozen globals with the (restored) clean env.
    policy._refreeze_compute_posture()
    policy._refreeze_payment_approval_flags()


def test_process_env_value_lands_after_load_env(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    assert policy.compute_posture() == 0  # frozen pre-load
    bootstrap.load_env()
    assert policy.compute_posture() == 2


def test_env_file_value_lands_after_load_env(monkeypatch):
    dotdir = os.environ["POLYROB_HOME"]
    os.makedirs(dotdir, exist_ok=True)
    with open(os.path.join(dotdir, ".env"), "w") as f:
        f.write("AGENT_COMPUTE_POSTURE=1\nPAYMENT_APPROVAL_MODE=auto\n")
    bootstrap.load_env()
    assert policy.compute_posture() == 1, (
        "owner home value must reach the frozen posture")
    assert policy.payment_approval_mode() == "auto"


def test_mid_session_env_mutation_stays_inert(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    bootstrap.load_env()
    assert policy.compute_posture() == 1
    # a later env mutation (the prompt-injection class) must NOT move the freeze
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    assert policy.compute_posture() == 1
    # even through another load_env call — the once-guard is consumed
    bootstrap.load_env()
    assert policy.compute_posture() == 1


def test_garbage_still_degrades_closed(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "9")
    bootstrap.load_env()
    assert policy.compute_posture() == 0


def test_refreeze_fires_for_the_api_entry_point_too(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    bootstrap.load_env()
    assert policy.payment_approval_mode() == "auto"


# --- 098 CO-09 -------------------------------------------------------------------------

def test_interactive_max_steps_from_env_file_applies(monkeypatch):
    import core.config_policy as cp
    from core.config_policy import goal_flags
    monkeypatch.delenv("INTERACTIVE_MAX_STEPS", raising=False)
    old = goal_flags.INTERACTIVE_MAX_STEPS
    dotdir = os.environ["POLYROB_HOME"]
    os.makedirs(dotdir, exist_ok=True)
    with open(os.path.join(dotdir, ".env"), "w") as f:
        f.write("INTERACTIVE_MAX_STEPS=200\n")
    try:
        bootstrap.load_env()
        assert goal_flags.interactive_max_steps() == 200
        assert cp.interactive_max_steps() == 200
    finally:
        os.environ.pop("INTERACTIVE_MAX_STEPS", None)
        goal_flags.INTERACTIVE_MAX_STEPS = old


def test_no_module_copies_interactive_max_steps():
    """098 CO-09: callers use the accessor; no module keeps a copy of the value."""
    import subprocess
    out = subprocess.run(
        ["git", "grep", "-n", "INTERACTIVE_MAX_STEPS", "--", "*.py", ":!tests",
         ":!core/config_policy/goal_flags.py", ":!core/flags_catalog.py", ":!scripts"],
        capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))))
    code_refs = [ln for ln in out.stdout.splitlines()
                 if "import" in ln and "INTERACTIVE_MAX_STEPS" in ln.split("#")[0]]
    assert code_refs == []


def test_no_frozen_value_reexports():
    assert not [n for n in vars(policy) if n.startswith("_FROZEN")
                or n == "_COMPUTE_POSTURE_FROZEN"]
