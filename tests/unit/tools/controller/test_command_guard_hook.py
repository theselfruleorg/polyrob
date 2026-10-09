"""073 W2: the shell command guard on the controller path.

The pre hook refuses the floor always and an unattended dangerous command when
shell_run is gated (no owner ask is filed). The approval exemption lets a SAFE
command skip the owner wait, so a gated shell_run asks only for the dangerous
class; SHELL_APPROVAL_MODE=every restores the wait on every call.
"""
import types

import pytest

from tools.controller import approval
from tools.controller.command_guard_hook import (
    make_command_guard_hook, make_shell_command_exemption, shell_approval_mode)


def _ctx(sid="s1"):
    return types.SimpleNamespace(session_id=sid, user_id="local")


@pytest.mark.asyncio
async def test_floor_is_refused_even_ungated():
    hook = make_command_guard_hook(shell_gated=False)
    assert "floor" in await hook("shell_run", {"command": "rm -rf /"}, _ctx())


@pytest.mark.asyncio
@pytest.mark.parametrize("action,params", [
    ("coding_run_tests", {"command": "rm -rf /"}),
    ("code_execution_run_code", {"language": "bash", "code": "rm -rf /"}),
])
async def test_alternate_execution_actions_obey_floor(action, params):
    hook = make_command_guard_hook(shell_gated=False)
    assert "floor" in await hook(action, params, _ctx())


@pytest.mark.asyncio
async def test_unattended_python_run_code_is_not_refused_by_the_shell_rule(monkeypatch):
    """A Python program is not a shell line: the shell gate does not refuse it on an
    unattended run; run_code's own sandbox/approval gate decides."""
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    params = {"language": "python", "code": "print(1)"}
    hook = make_command_guard_hook(shell_gated=True)
    assert await hook("code_execution_run_code", params, _ctx()) is None
    # Own gate: not exempted. Gate inherited from shell_run: exempted.
    assert make_shell_command_exemption()("code_execution_run_code", params) is None
    inherited = make_shell_command_exemption(inherited=("code_execution_run_code",))
    assert inherited("code_execution_run_code", params)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["bash", "sh", "shell", "SHELL", "zsh"])
async def test_every_shell_language_of_run_code_is_classified(language, monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    hook = make_command_guard_hook(shell_gated=True)
    assert "floor" in await hook("code_execution_run_code",
                                 {"language": language, "code": "echo x\n\nrm -rf /etc"}, _ctx())
    assert "unattended" in await hook("code_execution_run_code",
                                      {"language": language, "code": "docker ps"}, _ctx())


def test_inherited_test_runs_follow_the_shell_rule():
    ex = make_shell_command_exemption("per_command",
                                      inherited=("coding_run_tests", "code_execution_run_code"))
    for cmd in ("pytest -q", "python -m pytest tests", "uv run pytest", "bash build.sh"):
        assert ex("coding_run_tests", {"command": cmd}), cmd
    assert ex("coding_run_tests", {"command": "timeout 5 polyrob approvals approve --all"}) is None
    # an action with its OWN gate keeps it
    assert make_shell_command_exemption("per_command")("coding_run_tests",
                                                       {"command": "pytest -q"}) is None


def test_default_test_run_without_a_command_is_judged_as_pytest():
    """run_tests with no command runs `pytest -q`: that default must follow the
    shell rule too, not wait on the owner because the command text is empty."""
    ex = make_shell_command_exemption("per_command", inherited=("coding_run_tests",))
    assert ex("coding_run_tests", {})
    assert ex("coding_run_tests", {"command": None})
    # an explicit dangerous command still reaches the owner
    assert ex("coding_run_tests", {"command": "polyrob approvals approve --all"}) is None


@pytest.mark.asyncio
async def test_other_actions_pass():
    hook = make_command_guard_hook(shell_gated=True)
    assert await hook("git_push", {"command": "rm -rf /"}, _ctx()) is None


@pytest.mark.asyncio
async def test_unattended_dangerous_is_refused_when_gated(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    hook = make_command_guard_hook(shell_gated=True)
    reason = await hook("shell_run", {"command": "rm -rf ../build"}, _ctx())
    assert reason and "unattended" in reason
    # ...but a safe command in the same run passes
    assert await hook("shell_run", {"command": "ls"}, _ctx()) is None


@pytest.mark.asyncio
async def test_attended_dangerous_goes_on_to_the_approval_hook(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    hook = make_command_guard_hook(shell_gated=True)
    assert await hook("shell_run", {"command": "rm -rf ../build"}, _ctx()) is None


@pytest.mark.asyncio
async def test_unattended_dangerous_ungated_runs_in_the_sandbox(monkeypatch):
    """Posture 1 (shell_run not gated): the sandbox keeps today's behaviour."""
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    hook = make_command_guard_hook(shell_gated=False)
    assert await hook("shell_run", {"command": "rm -rf ../build"}, _ctx()) is None


def test_exemption_per_command():
    ex = make_shell_command_exemption("per_command")
    assert ex("shell_run", {"command": "ls -la"})
    assert ex("shell_run", {"command": "rm -rf ../build"}) is None
    assert ex("shell_run", {"command": ""}) is None
    assert ex("shell_run", {}) is None
    assert ex("git_push", {"command": "ls"}) is None


def test_exemption_every_mode_never_exempts():
    ex = make_shell_command_exemption("every")
    assert ex("shell_run", {"command": "ls"}) is None


@pytest.mark.parametrize("raw,mode", [(None, "per_command"), ("every", "every"),
                                      ("PER_COMMAND", "per_command"), ("bogus", "every")])
def test_mode_parse(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv("SHELL_APPROVAL_MODE", raising=False)
    else:
        monkeypatch.setenv("SHELL_APPROVAL_MODE", raw)
    assert shell_approval_mode() == mode


@pytest.mark.asyncio
async def test_gated_shell_run_asks_only_for_the_dangerous_class():
    """End to end through make_approval_hook with a DENY provider: a safe command
    is exempt (allowed), a dangerous one reaches the provider (denied)."""
    hook = approval.make_approval_hook(
        approval.DenyByDefaultApprover(), {"shell_run"},
        exempt_fn=make_shell_command_exemption("per_command"))
    assert await hook("shell_run", {"command": "pytest -q"}, None) is None
    assert await hook("shell_run", {"command": "git push --force"}, None) is not None


# --- owner lists (SHELL_ALLOW / SHELL_DENY, shell.allow / shell.deny) ---------------

from core.security.command_guard import glob_for, matches_any, parse_globs


def test_glob_helpers():
    assert parse_globs("git push*, ,make *") == ("git push*", "make *")
    assert parse_globs(["a", "a", " b "]) == ("a", "b")
    assert matches_any("git   push origin main", ["git push*"]) == "git push*"
    assert matches_any("ls", ["git *"]) is None
    g = glob_for("rm -rf build/*")
    assert matches_any("rm -rf build/*", [g]) and not matches_any("rm -rf build/x", [g])


@pytest.mark.asyncio
async def test_deny_list_refuses_even_safe_and_ungated():
    hook = make_command_guard_hook(shell_gated=False, deny_globs=("git push*",))
    assert "deny list" in await hook("shell_run", {"command": "git push origin"}, _ctx())
    assert await hook("shell_run", {"command": "git status"}, _ctx()) is None


def test_allow_list_exempts_dangerous_but_never_floor():
    ex = make_shell_command_exemption("every", allow_globs=("rm -rf ../build*", "rm -rf /"))
    assert "allow list" in ex("shell_run", {"command": "rm -rf ../build"})
    assert ex("shell_run", {"command": "rm -rf /"}) is None  # floor is never exempt
    assert ex("shell_run", {"command": "ls"}) is None        # every: only the list exempts


def test_shell_lists_union_env_and_prefs(tmp_path, monkeypatch):
    from core.prefs import write_preference
    from tools.controller.command_guard_hook import shell_lists
    monkeypatch.setenv("SHELL_DENY", "curl *")
    monkeypatch.setenv("SHELL_ALLOW", "make *")
    ok, err = write_preference(tmp_path, "u1", "shell.deny", ["wget *"])
    assert ok, err
    ok, err = write_preference(tmp_path, "u1", "shell.allow", ["npm *"])
    assert ok, err
    allow, deny = shell_lists("u1", tmp_path)
    assert set(deny) == {"curl *", "wget *"} and set(allow) == {"make *", "npm *"}


# --- smart triage (D3) ---------------------------------------------------------------

class _LLM:
    def __init__(self, word):
        self.word = word

    async def ainvoke(self, msgs):
        return types.SimpleNamespace(content=self.word)


def _factory(word):
    async def _f():
        return _LLM(word)
    return _f


def _meta_ctx():
    return types.SimpleNamespace(session_id="s1", user_id="local", metadata={})


@pytest.mark.asyncio
async def test_smart_approve_in_sandbox_exempts(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    monkeypatch.setattr("tools.shell.backend_pool.host_selected", lambda ctx: False)
    ctx = _meta_ctx()
    hook = make_command_guard_hook(shell_gated=True, mode="smart", llm_factory=_factory("APPROVE"))
    assert await hook("shell_run", {"command": "docker ps"}, ctx) is None
    ex = make_shell_command_exemption("smart")
    assert "smart" in ex("shell_run", {"command": "docker ps"}, ctx)
    assert ex("shell_run", {"command": "docker rm -f x"}, ctx) is None  # only that command


@pytest.mark.asyncio
async def test_smart_never_approves_on_the_host(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    monkeypatch.setattr("tools.shell.backend_pool.host_selected", lambda ctx: True)
    ctx = _meta_ctx()
    hook = make_command_guard_hook(shell_gated=True, mode="smart", llm_factory=_factory("APPROVE"))
    assert await hook("shell_run", {"command": "docker ps"}, ctx) is None
    assert make_shell_command_exemption("smart")("shell_run", {"command": "docker ps"}, ctx) is None


@pytest.mark.asyncio
async def test_smart_deny_refuses_and_garbage_escalates(monkeypatch):
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    monkeypatch.setattr("tools.shell.backend_pool.host_selected", lambda ctx: False)
    deny = make_command_guard_hook(shell_gated=True, mode="smart", llm_factory=_factory("DENY."))
    assert "smart shell triage" in await deny("shell_run", {"command": "docker ps"}, _meta_ctx())
    ctx = _meta_ctx()
    meh = make_command_guard_hook(shell_gated=True, mode="smart", llm_factory=_factory("maybe"))
    assert await meh("shell_run", {"command": "docker ps"}, ctx) is None
    assert ctx.metadata == {}


@pytest.mark.asyncio
async def test_smart_llm_failure_escalates():
    async def _boom():
        raise RuntimeError("no model")
    from tools.controller.command_guard_hook import smart_triage
    assert await smart_triage("docker ps", "x", _boom) == "escalate"


# --- the interactive ladder is keyed by the command --------------------------------

@pytest.mark.asyncio
async def test_ladder_session_is_per_command(tmp_path):
    from tools.controller.approval_interactive import InteractiveCLIApprover
    answers = iter(["s"])
    ap = InteractiveCLIApprover(input_fn=lambda p: next(answers), user_id="u1", home_dir=tmp_path)
    assert await ap.request("shell_run", {"command": "docker ps"}, None) is True
    assert await ap.request("shell_run", {"command": "docker  ps"}, None) is True  # same line
    answers2 = iter(["d"])
    ap._input_fn = lambda p: next(answers2)
    assert await ap.request("shell_run", {"command": "docker rm -f x"}, None) is False


@pytest.mark.asyncio
async def test_ladder_never_denies_the_command_not_the_tool(tmp_path):
    from core.prefs import load_preferences
    from tools.controller.approval_interactive import InteractiveCLIApprover
    ap = InteractiveCLIApprover(input_fn=lambda p: "n", user_id="u1", home_dir=tmp_path)
    assert await ap.request("shell_run", {"command": "git push --force"}, None) is False
    prefs = load_preferences(tmp_path, "u1")
    assert prefs.get("shell.deny") == ["git push --force"]
    assert "shell_run" not in (prefs.get("approvals.deny") or [])


@pytest.mark.asyncio
async def test_ladder_always_queues_allow_for_review(tmp_path):
    from core.prefs import list_pending_pref_changes, load_preferences
    from tools.controller.approval_interactive import InteractiveCLIApprover
    ap = InteractiveCLIApprover(input_fn=lambda p: "a", user_id="u1", home_dir=tmp_path)
    assert await ap.request("shell_run", {"command": "docker ps"}, None) is True
    assert not load_preferences(tmp_path, "u1").get("shell.allow")  # not written directly
    pending = list_pending_pref_changes("u1", tmp_path)
    assert any("shell.allow" in str(p) for p in pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["process_write", "process_submit"])
async def test_text_typed_into_a_job_meets_the_floor_and_the_deny_list(action):
    """A background job may be a shell (`script`, `ssh`, an approved `bash`): the
    text typed into its stdin must not skip the floor or the owner's deny list."""
    hook = make_command_guard_hook(shell_gated=False, deny_globs=("git push*",))
    assert "floor" in await hook(action, {"job_id": "j1", "data": "rm -rf ~\n"}, _ctx())
    assert "deny list" in await hook(action, {"job_id": "j1", "data": "git push origin\n"}, _ctx())
    # Ordinary input (a REPL line, an answer, a dangerous-class line) passes.
    for data in ("print(1)\n", "y\n", "", "docker ps\n"):
        assert await hook(action, {"job_id": "j1", "data": data}, _ctx()) is None
