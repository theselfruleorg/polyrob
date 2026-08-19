"""026 P1.1 — the import-frozen policy flags must see the env-file ladder.

Failure mode A: ``core/bootstrap.py`` imports ``core.config_policy`` at module
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

import core.bootstrap as bootstrap
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
    bootstrap.load_env(local_mode=True)
    assert policy.compute_posture() == 2


def test_env_file_value_lands_after_load_env(monkeypatch):
    proj = tmp = os.getcwd()
    dotdir = os.path.join(tmp, ".polyrob")
    os.makedirs(dotdir, exist_ok=True)
    with open(os.path.join(dotdir, ".env"), "w") as f:
        f.write("AGENT_COMPUTE_POSTURE=1\nPAYMENT_APPROVAL_MODE=auto\n")
    bootstrap.load_env(local_mode=True)
    assert policy.compute_posture() == 1, (
        f"file value must reach the frozen posture (project {proj})")
    assert policy.payment_approval_mode() == "auto"


def test_mid_session_env_mutation_stays_inert(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    bootstrap.load_env(local_mode=True)
    assert policy.compute_posture() == 1
    # a later env mutation (the prompt-injection class) must NOT move the freeze
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    assert policy.compute_posture() == 1
    # even through another load_env call — the once-guard is consumed
    bootstrap.load_env(local_mode=True)
    assert policy.compute_posture() == 1


def test_garbage_still_degrades_closed(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "9")
    bootstrap.load_env(local_mode=True)
    assert policy.compute_posture() == 0


def test_refreeze_fires_for_server_mode_too(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    bootstrap.load_env(local_mode=False)
    assert policy.payment_approval_mode() == "auto"
