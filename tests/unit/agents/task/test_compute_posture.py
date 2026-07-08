"""WS-0 (computer-use parity): AGENT_COMPUTE_POSTURE resolver + gate predicate.

The compute posture is a third capability axis (orthogonal to POLYROB_LOCAL and
AUTONOMY_POSTURE): how much host/compute capability the agent has (0 confined,
1 sandbox-dev, 2 self-maintain, 3 host). Security contract under test:

- default CLOSED: unset/garbage/out-of-range -> 0 (never rounds UP to a higher tier);
- FROZEN at import: a mid-process os.environ mutation (e.g. a prompt-injected write
  that reached an env-mutating surface) can never raise the running posture;
- the single gate predicate `compute_posture_allows(ctx, N)` requires posture >= N
  AND an owner-tier principal AND not-leaf/sub-agent AND not a forged
  (self-wake/delegation-result) turn. Autonomous goal/cron sessions of the owner
  tenant PASS (WS-8 provisions them with the compute toolset); only the forged
  re-entry turn-kind stamp denies.
"""
import pytest

import agents.task.constants as c
from tools.controller.execution_context import ActionExecutionContext


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL",
        "POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
        "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    # Teardown-order landmine: this post-yield code runs BEFORE monkeypatch.undo
    # (monkeypatch is our dependency, so it tears down after us). Refreezing while
    # a test's setenv("AGENT_COMPUTE_POSTURE", "3") is still live would freeze
    # posture 3 process-wide and leak it into every later test (it flipped
    # code_exec_docker_persistent's default ON in unrelated suites). Clear the
    # env explicitly first, then refreeze to the true default.
    import os
    os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    c._refreeze_compute_posture_for_tests()


def _freeze(monkeypatch, value):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", value)
    c._refreeze_compute_posture_for_tests()


def _ctx(**kw):
    """A genuine owner-steered main-agent context (clean env: owner principal is
    the instance default 'rob', so user_id='rob' is the owner tenant)."""
    defaults = dict(
        role="orchestrator", is_sub_agent=False, user_id="rob",
        session_id="s1", metadata={"turn_kind": None},
    )
    defaults.update(kw)
    return ActionExecutionContext(**defaults)


# --- resolver: default closed, never rounds up ---------------------------------

def test_default_posture_is_zero():
    assert c.compute_posture() == 0


@pytest.mark.parametrize("raw", ["", "banana", "-1", "9", "1.5", "none"])
def test_garbage_and_out_of_range_degrade_to_zero(monkeypatch, raw):
    # A typo/garbage value must NEVER grant a higher tier than explicitly set.
    _freeze(monkeypatch, raw)
    assert c.compute_posture() == 0


@pytest.mark.parametrize("raw,expect", [("0", 0), ("1", 1), (" 2 ", 2), ("3", 3)])
def test_valid_postures_resolve(monkeypatch, raw, expect):
    _freeze(monkeypatch, raw)
    assert c.compute_posture() == expect


def test_posture_is_frozen_against_mid_process_env_mutation(monkeypatch):
    assert c.compute_posture() == 0
    # simulate a prompt-injected env write AFTER process start: must be ignored
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    assert c.compute_posture() == 0


# --- gate predicate --------------------------------------------------------------

def test_gate_denies_everything_at_default_posture():
    assert c.compute_posture_allows(_ctx(), 1) is False
    assert c.compute_posture_allows(_ctx(), 2) is False
    assert c.compute_posture_allows(_ctx(), 3) is False


def test_gate_allows_owner_orchestrator_at_sufficient_posture(monkeypatch):
    _freeze(monkeypatch, "1")
    assert c.compute_posture_allows(_ctx(), 1) is True
    assert c.compute_posture_allows(_ctx(), 2) is False  # insufficient tier
    _freeze(monkeypatch, "2")
    assert c.compute_posture_allows(_ctx(), 2) is True


def test_gate_min_posture_zero_is_the_unconditional_baseline(monkeypatch):
    # Posture-0 capabilities are gated by their own existing mechanisms.
    assert c.compute_posture_allows(_ctx(), 0) is True


def test_gate_denies_leaf_and_sub_agent(monkeypatch):
    _freeze(monkeypatch, "3")
    assert c.compute_posture_allows(_ctx(role="leaf"), 1) is False
    assert c.compute_posture_allows(_ctx(is_sub_agent=True), 1) is False


@pytest.mark.parametrize("kind", ["self_wake", "delegation_result"])
def test_gate_denies_forged_turns(monkeypatch, kind):
    _freeze(monkeypatch, "3")
    assert c.compute_posture_allows(_ctx(metadata={"turn_kind": kind}), 1) is False


def test_gate_denies_non_owner_and_anonymous(monkeypatch):
    _freeze(monkeypatch, "3")
    assert c.compute_posture_allows(_ctx(user_id="u_stranger"), 1) is False
    assert c.compute_posture_allows(_ctx(user_id=""), 1) is False
    assert c.compute_posture_allows(_ctx(user_id=None), 1) is False


def test_gate_denies_missing_context(monkeypatch):
    _freeze(monkeypatch, "3")
    assert c.compute_posture_allows(None, 1) is False


def test_gate_local_bypass_scoped_to_local_operator_tenant(monkeypatch):
    """POLYROB_LOCAL on a network-facing box (prod Telegram) must NOT auto-own a
    forgeable sender uid; only the CLI's 'local' operator tenant gets the bypass."""
    _freeze(monkeypatch, "1")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert c.compute_posture_allows(_ctx(user_id="local"), 1) is True
    assert c.compute_posture_allows(_ctx(user_id="28436760"), 1) is False


def test_gate_local_tenant_denied_without_local_mode(monkeypatch):
    _freeze(monkeypatch, "1")
    assert c.compute_posture_allows(_ctx(user_id="local"), 1) is False


def test_gate_explicit_owner_principal_binding_wins(monkeypatch):
    _freeze(monkeypatch, "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    assert c.compute_posture_allows(_ctx(user_id="alice"), 1) is True
    # instance-id default no longer matches once an explicit owner is bound
    assert c.compute_posture_allows(_ctx(user_id="rob"), 1) is False


def test_gate_allows_owner_tenant_autonomous_session(monkeypatch):
    """An autonomous goal/cron session (owner tenant, orchestrator role, NO forged
    turn-kind stamp) must pass — WS-8 provisions those runs with the compute
    toolset; the acceptance goal itself is such a session. Only the forged
    re-entry stamp (self_wake/delegation_result) denies."""
    _freeze(monkeypatch, "1")
    ctx = _ctx(session_id="goal-run-1", metadata={})  # no turn_kind key at all
    assert c.compute_posture_allows(ctx, 1) is True
