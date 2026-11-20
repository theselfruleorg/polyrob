"""WS-5: the `self_env` self-maintenance tool (posture 2).

Distinct verbs (never raw bash): read_source / restart_service. Every verb is gated
compute_posture_allows(ctx, 2) AND (via the Controller's posture-2 wiring) approval.
read_source is realpath-confined to the install tree and HARD-DENIES env/config
files; restart_service refuses when the process is not supervised. 120 W7:
request_update is the restart's sibling — owner-approved, the owner's /update rule
(core.lifecycle_ops.update_target), one lifecycle request of kind update.
"""
import logging
from pathlib import Path

import pytest

import agents.task.constants as c
from core.identity import LocalIdentity
from tools.self_env.tool import SelfEnvTool, ReadSourceParams, RestartParams, UpdateParams
from agents.task.loop_types.execution_context import ActionExecutionContext


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "POLYROB_INSTALL_TREE", "POLYROB_SUPERVISED"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    # LIFO landmine (see the docker-test twins + inbox 2026-07-14): this teardown
    # runs BEFORE monkeypatch reverts env, so refreezing first re-snapshots a
    # test's posture and leaks it into every later test in the process. Pop the
    # envs explicitly, THEN refreeze.
    import os as _os
    _os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    _os.environ.pop("POLYROB_LOCAL", None)
    _os.environ.pop("POLYROB_OWNER_USER_ID", None)
    _os.environ.pop("POLYROB_INSTALL_TREE", None)
    _os.environ.pop("POLYROB_SUPERVISED", None)
    c._refreeze_compute_posture_for_tests()


def _tool(install_root, events=None):
    t = object.__new__(SelfEnvTool)
    t.logger = logging.getLogger("self-env-test")
    t._install_root_override = str(install_root)
    if events is not None:
        t._emit = lambda **kw: events.append(kw)
    return t


def _owner_ctx(**kw):
    # The owner tenant. ⚠️ This was DEFAULT_INSTANCE_ID until 2026-09-15, when the
    # owner principal's unbound fallback stopped being the instance id — an owner
    # context built from the instance id is now a STRANGER to
    # `compute_posture_allows`, so every posture-2 verb below would refuse. The
    # autouse `_clean` fixture deletes POLYROB_OWNER_USER_ID, so the unbound
    # answer (`local`) is the right stand-in here.
    d = dict(role="orchestrator", is_sub_agent=False, user_id=LocalIdentity.USER_ID,
             session_id="s1", metadata={"turn_kind": None})
    d.update(kw)
    return ActionExecutionContext(**d)


def _posture(monkeypatch, v):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", v)
    c._refreeze_compute_posture_for_tests()


# --- gating ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_denied_below_posture_2(monkeypatch, tmp_path):
    _posture(monkeypatch, "1")  # posture 1 is NOT enough for self_env
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="x.py"),
                                       execution_context=_owner_ctx())
    assert res.error and "posture" in res.error.lower()


@pytest.mark.asyncio
async def test_denied_for_leaf_at_posture_2(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="x.py"),
                                       execution_context=_owner_ctx(role="leaf"))
    assert res.error


# --- read confinement ------------------------------------------------------

@pytest.mark.asyncio
async def test_read_source_within_tree(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "mod.py").write_text("print('hi')\n")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="mod.py"),
                                       execution_context=_owner_ctx())
    assert not res.error and "print('hi')" in res.extracted_content


@pytest.mark.asyncio
async def test_read_source_rejects_path_outside_tree(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="../../../etc/hosts"),
                                       execution_context=_owner_ctx())
    assert res.error and ("outside" in res.error.lower() or "escape" in res.error.lower()
                          or "confine" in res.error.lower())


@pytest.mark.asyncio
async def test_read_source_hard_denies_env_file(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env.production").write_text("SECRET=1\n")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="config/.env.production"),
                                       execution_context=_owner_ctx())
    assert res.error and ("credential" in res.error.lower() or "config" in res.error.lower()
                          or "secret" in res.error.lower())


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [
    "data/database/bot.db",   # main app DB — may hold tokens/PII
    "data/wallet.sqlite",     # wallet material
    "data/database/bot.db-wal",
    ".git/config",            # remote URLs / hooks
])
async def test_confine_denies_in_tree_secrets_and_git(monkeypatch, tmp_path, rel):
    """self_env must not read in-tree secrets (bot.db/*.sqlite) or the .git dir
    even though they live under the install tree — the narrow credential list
    missed them."""
    _posture(monkeypatch, "2")
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("secret")
    t = _tool(tmp_path)
    r_read = await t.self_env_read_source(ReadSourceParams(path=rel),
                                          execution_context=_owner_ctx())
    assert r_read.error, f"read_source should deny {rel}"


@pytest.mark.asyncio
async def test_read_source_emits_audit_on_confine_denied(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    events = []
    t = _tool(tmp_path, events=events)
    await t.self_env_read_source(ReadSourceParams(path="../../etc/passwd"),
                                 execution_context=_owner_ctx())
    assert any(e.get("action") == "read_source" and e.get("ok") is False for e in events)


@pytest.mark.asyncio
async def test_read_source_hard_denies_polyrob_env(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "polyrob.env").write_text("AGENT_COMPUTE_POSTURE=3\n")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="polyrob.env"),
                                       execution_context=_owner_ctx())
    assert res.error


# --- restart_service -------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_service_refuses_without_a_supervised_service(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    t = _tool(tmp_path)
    res = await t.self_env_restart_service(RestartParams(), execution_context=_owner_ctx())
    assert res.error and "nothing would start it again" in res.error


@pytest.mark.asyncio
async def test_restart_service_writes_the_drained_restart_request(monkeypatch, tmp_path):
    """077: the call runs only after the owner approved it (always_queued); it then
    asks the running service for a drained restart — it restarts nothing itself."""
    from core import lifecycle as lc
    _posture(monkeypatch, "2")
    home = tmp_path / "data"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setenv("INVOCATION_ID", "unit")
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)  # a pytest shell is a .scope
    lc.record_boot(data_home=home)
    t = _tool(tmp_path)
    res = await t.self_env_restart_service(RestartParams(reason="new MCP server"),
                                           execution_context=_owner_ctx())
    assert not res.error and "Do not say it restarted" in res.extracted_content
    req = lc.read_request(data_home=home)
    assert req.kind == lc.KIND_RESTART and req.reason == "new MCP server"
    assert req.origin["surface"] == "agent"


# --- request_update (120 W7) -----------------------------------------------------

def _supervised(monkeypatch, tmp_path):
    from core import lifecycle as lc
    home = tmp_path / "data"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setenv("INVOCATION_ID", "unit")
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)
    lc.record_boot(data_home=home)
    return home


def _check(monkeypatch, **info):
    """A fake `polyrob update --check --json` (never a subprocess, never the network)."""
    from core import lifecycle_ops
    seen = []

    async def _c(channel):
        seen.append(channel)
        base = {"current": "1.3.0", "latest": "v1.4.0", "update_available": True,
                "method": "editable_git", "error": None}
        base.update(info)
        return base, ""
    monkeypatch.setattr(lifecycle_ops, "_update_check", _c)
    return seen


@pytest.mark.asyncio
async def test_request_update_is_denied_below_posture_2(monkeypatch, tmp_path):
    from core import lifecycle as lc
    _posture(monkeypatch, "1")
    home = _supervised(monkeypatch, tmp_path)
    _check(monkeypatch)
    res = await _tool(tmp_path).self_env_request_update(UpdateParams(),
                                                        execution_context=_owner_ctx())
    assert res.error and "posture" in res.error.lower()
    assert lc.read_request(data_home=home) is None


@pytest.mark.asyncio
async def test_request_update_writes_the_update_request(monkeypatch, tmp_path):
    from core import lifecycle as lc
    _posture(monkeypatch, "2")
    home = _supervised(monkeypatch, tmp_path)
    seen = _check(monkeypatch)
    events = []
    res = await _tool(tmp_path, events=events).self_env_request_update(
        UpdateParams(channel="pre", reason="a fix the owner asked for"),
        execution_context=_owner_ctx())
    assert not res.error, res.error
    assert "1.3.0 → 1.4.0" in res.extracted_content
    assert "Do not say it updated" in res.extracted_content
    req = lc.read_request(data_home=home)
    assert req.kind == lc.KIND_UPDATE and req.channel == "pre" and seen == ["pre"]
    assert req.target_version == "v1.4.0" and req.origin["surface"] == "agent"
    assert req.requested_by == "the agent (owner-approved)"
    assert any(e.get("action") == "request_update" and e.get("ok") for e in events)
    # A second call returns the open request; it never queues a second one.
    again = await _tool(tmp_path).self_env_request_update(UpdateParams(),
                                                          execution_context=_owner_ctx())
    assert "already open" in again.extracted_content
    assert lc.read_request(data_home=home).id == req.id


@pytest.mark.asyncio
@pytest.mark.parametrize("info, says", [
    ({"update_available": False, "latest": "1.3.0"}, "on the latest version"),
    ({"method": "pip"}, "does not update itself"),
    ({"error": "rate limited"}, "could not check for an update"),
])
async def test_request_update_refuses_with_the_owner_rule(monkeypatch, tmp_path, info, says):
    """The agent's refusal is the owner's /update text, from the one rule."""
    from core import lifecycle as lc
    from core.lifecycle_ops import update_reply
    _posture(monkeypatch, "2")
    home = _supervised(monkeypatch, tmp_path)
    _check(monkeypatch, **info)
    res = await _tool(tmp_path).self_env_request_update(UpdateParams(),
                                                        execution_context=_owner_ctx())
    seat = await update_reply("u1", str(home), ["go"], {"surface": "telegram"})
    assert res.error == f"update not requested: {seat}" and says in seat
    assert lc.read_request(data_home=home) is None


@pytest.mark.asyncio
async def test_request_update_refuses_without_a_supervised_service(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    seen = _check(monkeypatch)
    res = await _tool(tmp_path).self_env_request_update(UpdateParams(),
                                                        execution_context=_owner_ctx())
    assert res.error and "update myself" in res.error and seen == []


def test_request_update_rides_the_restart_lane():
    """Owner-approved even under AUTONOMY_MODE=autonomous, tainted turns refused."""
    from dataclasses import replace
    from core.verb_policy import policy_for
    update, restart = policy_for("self_env_request_update"), policy_for("self_env_restart_service")
    assert update is not None and "always_queued" in update.approval
    assert replace(update, name=restart.name) == restart


# --- delegation / registration guardrails ----------------------------------------

def test_self_env_is_delegation_blocked():
    from core.delegation_policy import DELEGATE_BLOCKED_TOOLS
    assert "self_env" in DELEGATE_BLOCKED_TOOLS


def test_self_env_registered_only_at_posture_2(monkeypatch):
    from tools.self_env import self_env_enabled
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    assert self_env_enabled() is False
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    c._refreeze_compute_posture_for_tests()
    assert self_env_enabled() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", ["core/wallet/authority.py", "core/money/authority.py",
                                 "core/money/authorize.py"])
async def test_read_source_refuses_the_money_kernel(monkeypatch, tmp_path, rel):
    """067 P1b moved the refusals from core/wallet/ to core/money/; the
    install-tree confinement follows them."""
    _posture(monkeypatch, "2")
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("X = 1\n")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path=rel),
                                       execution_context=_owner_ctx())
    assert res.error and "refusing to patch" in res.error
