"""114 W2: the script job — the agent's own file on a schedule, no model turn."""
import os
import shutil
from datetime import datetime
from types import SimpleNamespace

import pytest

from cron import read_job as S
from cron.jobs import CronJob, CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService
from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend
from tools.code_exec.result import ExecutionResult

SPEC = {"path": "jobs/collect.py"}


def _job(payload):
    return CronJob(id="j1", user_id="alice", task="t", schedule_spec="30m", next_run_at=None,
                   payload=payload)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    (ws / "jobs").mkdir(parents=True)
    monkeypatch.setattr(S, "_script_workspace", lambda: os.path.realpath(str(ws)))
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason",
                        lambda: None)
    monkeypatch.setattr("tools.code_exec.sandbox_guard.local_host_exec_refusal", lambda ctx: None)
    return ws


# -- the spec -----------------------------------------------------------------

def test_the_spec_is_normalized():
    assert S.validate_script({"path": " jobs/collect.py "}) == {
        "path": "jobs/collect.py", "args": [], "timeout_s": S.SCRIPT_DEFAULT_TIMEOUT_S,
        "deliver_on": "alert"}


@pytest.mark.parametrize("bad", [
    None, "jobs/collect.py", {}, {"path": ""}, {"path": "/etc/passwd.py"},
    {"path": "../x.py"}, {"path": "a/../../x.py"}, {"path": "~/x.py"}, {"path": "-x.py"},
    {"path": "jobs/collect"}, {"path": "jobs/collect.rb"}, {"path": "a//b.py"},
    {"path": "a.py", "args": "x"}, {"path": "a.py", "args": [1]},
    {"path": "a.py", "args": ["x"] * (S.SCRIPT_MAX_ARGS + 1)},
    {"path": "a.py", "timeout_s": 0}, {"path": "a.py", "timeout_s": S.SCRIPT_MAX_TIMEOUT_S + 1},
    {"path": "a.py", "timeout_s": True}, {"path": "a.py", "deliver_on": "sometimes"},
    {"path": "a.py", "network": "host"},
])
def test_a_spec_that_cannot_run_is_refused(bad):
    with pytest.raises(S.ScriptJobError):
        S.validate_script(bad)


def test_the_launch_line_quotes_every_word():
    line = S.launch_line({"path": "jobs/my file.py", "args": ["a b", "$(id)", ";rm"]})
    assert line == "exec python3 './jobs/my file.py' 'a b' '$(id)' ';rm'"
    assert S.launch_line({"path": "run.sh", "args": []}) == "exec bash ./run.sh"


def test_a_link_that_leaves_the_workspace_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("print(1)")
    (ws / "link.py").symlink_to(outside)
    with pytest.raises(S.ScriptJobError, match="outside"):
        S.resolve_script(os.path.realpath(str(ws)), "link.py")
    with pytest.raises(S.ScriptJobError, match="not a file"):
        S.resolve_script(os.path.realpath(str(ws)), "none.py")


# -- where it can run ----------------------------------------------------------

def test_no_code_execution_means_no_script_job(monkeypatch):
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason",
                        lambda: "code execution is disabled on this server")
    assert "disabled" in S.script_refusal()


def test_a_per_session_workspace_means_no_script_job(monkeypatch):
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason",
                        lambda: None)
    monkeypatch.setattr(S, "_script_workspace", lambda: None)
    assert "durable workspace" in S.script_refusal()


# -- the run -------------------------------------------------------------------

class _Backend:
    def __init__(self, result):
        self.result, self.requests = result, []

    async def run(self, request):
        self.requests.append(request)
        return self.result


@pytest.mark.asyncio
async def test_the_request_is_one_bounded_run_in_the_workspace(workspace):
    (workspace / "jobs" / "collect.py").write_text("print('3 rows')")
    backend = _Backend(ExecutionResult(stdout="3 rows\n", exit_code=0))
    ok, text = await S.call_script({**SPEC, "args": ["--n", "3"], "timeout_s": 45},
                                   backend_factory=lambda: backend)
    assert (ok, text) == (True, "3 rows")
    req = backend.requests[0]
    assert req.language == "bash" and req.code == "exec python3 ./jobs/collect.py --n 3"
    assert req.workdir == os.path.realpath(str(workspace))
    assert req.timeout == 45 and req.ceiling == 45 and not req.dev_mode and not req.env


@pytest.mark.asyncio
@pytest.mark.parametrize("result,needle", [
    (ExecutionResult(stdout="partial", stderr="Traceback: boom", exit_code=3), "exit code 3"),
    (ExecutionResult(exit_code=None, timed_out=True), "timed out after 120 s"),
])
async def test_a_bad_exit_or_a_timeout_is_a_failure_with_the_reason(workspace, result, needle):
    (workspace / "jobs" / "collect.py").write_text("x")
    ok, text = await S.call_script(SPEC, backend_factory=lambda: _Backend(result))
    assert ok is False and needle in text and text.startswith("jobs/collect.py: ")
    if result.stderr:
        assert "Traceback: boom" in text and "partial" in text


@pytest.mark.asyncio
async def test_a_missing_file_or_a_refused_host_never_reaches_the_backend(workspace, monkeypatch):
    def never():
        raise AssertionError("the backend was built")
    ok, text = await S.call_script(SPEC, backend_factory=never)
    assert ok is False and "not a file" in text
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason",
                        lambda: "refused here")
    ok, text = await S.call_script(SPEC, backend_factory=never)
    assert ok is False and "refused here" in text


@pytest.mark.asyncio
@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("python3")),
                    reason="needs bash and python3")
async def test_a_real_script_runs_from_the_workspace_root(workspace):
    (workspace / "jobs" / "collect.py").write_text(
        "import os, sys\nopen('out.txt', 'w').write(sys.argv[1])\n"
        "print('ALERT', os.path.basename(os.getcwd()))\nsys.exit(int(sys.argv[2]))\n")
    ok, text = await S.call_script({**SPEC, "args": ["hello world", "0"]},
                                   backend_factory=LocalSubprocessBackend)
    assert (ok, text) == (True, "ALERT project")
    assert (workspace / "out.txt").read_text() == "hello world"
    ok, text = await S.call_script({**SPEC, "args": ["x", "4"]},
                                   backend_factory=LocalSubprocessBackend)
    assert ok is False and "exit code 4" in text


# -- delivery ------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("result,deliver_on,delivered", [
    (ExecutionResult(stdout="4 rows", exit_code=0), "alert", False),
    (ExecutionResult(stdout="ALERT: no rows", exit_code=0), "alert", True),
    (ExecutionResult(stderr="boom", exit_code=1), "alert", True),
    (ExecutionResult(stderr="boom", exit_code=1), "never", False),
    (ExecutionResult(stdout="4 rows", exit_code=0), "always", True),
])
async def test_delivery_follows_deliver_on(workspace, monkeypatch, result, deliver_on, delivered):
    (workspace / "jobs" / "collect.py").write_text("x")
    sent = []

    async def deliver(task_agent, job, text, **kw):
        sent.append((text, kw))
        return True
    monkeypatch.setattr("cron.delivery.deliver_result", deliver)
    job = _job({"script": {**SPEC, "deliver_on": deliver_on}, "deliver": "telegram"})
    ok, why = await S.run_script_job(None, job, backend_factory=lambda: _Backend(result))
    assert ok is result.ok and bool(sent) is delivered
    assert why == ("script_delivered" if delivered else "script")


# -- the runner and the service --------------------------------------------------

@pytest.fixture
def runner(tmp_path, monkeypatch):
    from cron import runner as r
    for k in ("AUTONOMY_HALT", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    events = []
    monkeypatch.setattr(r, "_cron_ev", lambda job, outcome, reason=None, **kw:
                        events.append((outcome, reason, kw)))

    class Agent:                       # any session creation is a failure here
        def __getattr__(self, name):
            raise AssertionError(f"the agent was invoked: {name}")
    return r.make_agent_runner(task_agent=Agent(), data_dir=str(tmp_path)), events


@pytest.mark.asyncio
async def test_the_runner_routes_a_script_job_without_the_agent(runner, monkeypatch):
    run, events = runner

    async def fake(task_agent, job, **kw):
        return True, "script"
    monkeypatch.setattr(S, "run_script_job", fake)
    assert await run(_job({"script": SPEC})) is True
    assert events == [("done", "script", {"script": "jobs/collect.py"})]


@pytest.mark.asyncio
async def test_a_failed_script_is_a_failed_run(runner, monkeypatch):
    run, _ = runner

    async def fake(task_agent, job, **kw):
        return False, "script_delivered"
    monkeypatch.setattr(S, "run_script_job", fake)
    job = _job({"script": SPEC})
    assert await run(job) is False
    assert job.run_note == {"outcome": "failed", "reason": "the script failed"}


def _svc(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    return CronService(store, now=lambda: datetime.fromisoformat("2026-09-29T12:00:00")), store


def test_the_service_stores_a_normalized_script_and_refuses_a_bad_one(tmp_path, workspace):
    svc, store = _svc(tmp_path)
    with pytest.raises(ScheduleError, match="not a file"):
        svc.schedule(task="collect", schedule_spec="1h", user_id="alice", payload={"script": SPEC})
    (workspace / "jobs" / "collect.py").write_text("print(1)")
    job = svc.schedule(task="collect", schedule_spec="1m", user_id="alice",
                       payload={"script": SPEC})          # no model turn: the 1-minute floor
    assert store.get(job.id).payload["script"]["timeout_s"] == S.SCRIPT_DEFAULT_TIMEOUT_S
    with pytest.raises(ScheduleError, match="relative path"):
        svc.schedule(task="collect", schedule_spec="1h", user_id="alice",
                     payload={"script": {"path": "../x.py"}})


def test_a_job_is_one_kind(tmp_path, workspace):
    svc, _ = _svc(tmp_path)
    (workspace / "jobs" / "collect.py").write_text("print(1)")
    read = {"verb": "defi_data.price", "params": {"chain": "base", "address": "0x" + "5" * 40}}
    with pytest.raises(ScheduleError, match="not both"):
        svc.schedule(task="t", schedule_spec="1h", user_id="alice",
                     payload={"script": SPEC, "read_verb": read})
    job = svc.schedule(task="t", schedule_spec="1h", user_id="alice", payload={"read_verb": read})
    with pytest.raises(ScheduleError, match="not both"):
        svc.edit(job.id, user_id="alice", payload_updates={"script": SPEC})


def test_an_edit_that_removes_the_script_keeps_the_model_floor(tmp_path, workspace):
    svc, _ = _svc(tmp_path)
    (workspace / "jobs" / "collect.py").write_text("print(1)")
    job = svc.schedule(task="t", schedule_spec="1m", user_id="alice", payload={"script": SPEC})
    with pytest.raises(ScheduleError, match="recurs faster"):
        svc.edit(job.id, user_id="alice", payload_updates={"script": None})


def test_a_script_job_takes_no_model_slot_and_does_not_repeat_after_a_cut():
    from core.cron_slots import MODEL_FREE_KEYS
    from cron.scheduler import cut_run_may_repeat
    assert "script" in MODEL_FREE_KEYS
    assert cut_run_may_repeat(_job({"script": SPEC})) is False


# -- the tool -------------------------------------------------------------------

def _tool(tmp_path):
    from tools.cronjob_tools import CronJobTool
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx():
    return SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                           metadata={}, session_id=None)


@pytest.mark.asyncio
async def test_the_tool_schedules_shows_and_clears_a_script(tmp_path, workspace, monkeypatch):
    from tools.cronjob_tools import CronEditAction, CronScheduleAction, CronShowAction
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    t = _tool(tmp_path)
    missing = await t.cronjob_schedule(
        CronScheduleAction(task="collect the rows", schedule="15m", script=SPEC),
        execution_context=_ctx())
    assert "not a file" in missing.error and not t._cron_service.list_jobs(user_id="u1")
    (workspace / "jobs" / "collect.py").write_text("print(1)")
    res = await t.cronjob_schedule(
        CronScheduleAction(task="collect the rows", schedule="15m", script=SPEC),
        execution_context=_ctx())
    assert res.error is None
    job = t._cron_service.list_jobs(user_id="u1")[0]
    assert job.payload["script"]["path"] == "jobs/collect.py"
    shown = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    assert "script (no model run): jobs/collect.py" in shown.extracted_content
    refused = await t.cronjob_schedule(
        CronScheduleAction(task="collect the rows", schedule="15m", script=SPEC,
                           expect={"acts": ["x_post"]}), execution_context=_ctx())
    assert refused.error
    cleared = await t.cronjob_edit(CronEditAction(job_id=job.id, script={}),
                                   execution_context=_ctx())
    assert cleared.error is None
    assert not t._cron_service.store.get(job.id).payload.get("script")


@pytest.mark.asyncio
async def test_the_tool_refuses_a_script_where_code_cannot_run(tmp_path, workspace, monkeypatch):
    from tools.cronjob_tools import CronScheduleAction
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.setattr("tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason",
                        lambda: "code execution is disabled on this server")
    t = _tool(tmp_path)
    res = await t.cronjob_schedule(
        CronScheduleAction(task="collect the rows", schedule="15m", script=SPEC),
        execution_context=_ctx())
    assert "disabled" in res.error and not t._cron_service.list_jobs(user_id="u1")
