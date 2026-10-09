"""The persistent `shell` tool (WS-2). NO ``from __future__ import annotations`` —
the action closure's Pydantic param model is introspected by the Registry (the
agent-upgrades-wave4 / GLM param_model landmine).
"""
import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.code_exec.limits import dev_exec_max_timeout_sec
from tools.controller.types import ActionResult
from tools.shell.state import ShellState
from tools.shell.discipline import background_nudge
from tools.shell.process_registry import get_process_registry

# Foreground ceiling: tools/code_exec/limits.py (SHELL_MAX_TIMEOUT_SEC, default 600)
# — shared with the dev-mode run_code cap. Default when the agent omits `timeout`
# (073 W3, cross-agent parity: 180):
_DEFAULT_TIMEOUT_SEC = 180.0


class ShellRunParams(BaseModel):
    """Parameters for ``shell_run`` (plain Pydantic — native-schema safe)."""

    command: str = Field(..., description="Shell command to run (bash). cwd/env persist across calls.")
    background: bool = Field(
        False,
        description="Run detached as a managed job (for servers / long jobs). Returns a "
                    "job id; use the `process` tool to poll / wait / read its log, and "
                    "write / submit / close to drive its stdin.",
    )
    timeout: Optional[float] = Field(
        None,
        description="Foreground wall-clock seconds (default 180, ceiling "
                    "SHELL_MAX_TIMEOUT_SEC = 600 unless set). A value ABOVE the ceiling "
                    "runs the command as a background job instead (with notify on exit "
                    "when available); then use `process` wait/poll. Not allowed with "
                    "background=True (a job runs until it exits or you kill it).",
    )
    workdir: Optional[str] = Field(
        None,
        description="Run THIS call (or this background job) in another directory "
                    "(absolute, or relative to the current one). It must exist. The "
                    "persisted cwd does not change.",
    )
    pty: bool = Field(
        False,
        description="Background only, host shell only: run the job on a pseudo-terminal "
                    "(REPLs, interactive installers). Type into it with `process` "
                    "write/submit.",
    )
    notify: Optional[str] = Field(
        None,
        description="Background only: 'exit' = you get a note when the job ends; "
                    "'pattern' = a note when an output line matches notify_pattern "
                    "(rate-limited, off after 5 notes) and when it ends.",
    )
    notify_pattern: Optional[str] = Field(
        None,
        description="Regex for notify='pattern' (matched per output line).",
    )


class ShellTool(BaseTool):
    """Run shell commands with cwd/env persistence — in the session's persistent
    sandbox, or (posture 3, 073 W1) on the host for the owner's terminal turn.

    Posture-gated: reachable only when ``compute_posture_allows(ctx, 1)`` (owner tenant,
    not leaf/sub-agent, not a forged turn) at AGENT_COMPUTE_POSTURE>=1. WHERE a command
    runs: ``backend_pool.resolve_shell_executor``. WHICH commands run: the floor of
    ``core.security.command_guard`` here (every backend), the dangerous class through
    the controller's approval hook (``tools/controller/command_guard_hook.py``).
    """

    def __init__(self, name: str = "shell", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._registry = get_process_registry()
        self._states: dict = {}         # (session_id, executor kind) -> ShellState
        self._lock = asyncio.Lock()

    @staticmethod
    def _allowed(execution_context) -> bool:
        from core.config_policy import compute_posture_allows_safe
        return compute_posture_allows_safe(execution_context, 1)

    def _state_for(self, session_id: str, executor=None) -> ShellState:
        kind = getattr(executor, "kind", "docker")
        key = (session_id, kind)
        st = self._states.get(key)
        if st is None:
            st = ShellState(cwd=getattr(executor, "default_cwd", None) or "/workspace")
            self._states[key] = st
        return st

    async def _resolve_executor(self, execution_context):
        """The ONE executor for this turn (``backend_pool.resolve_shell_executor``):
        the host at posture 3 for the owner's terminal turn, else the session's SHARED
        persistent sandbox — the SAME container the `process` tool polls, so a
        background job's pid is meaningful across both tools."""
        from tools.shell.backend_pool import resolve_shell_executor
        return await resolve_shell_executor(execution_context)

    async def _loopback_note(self, session_id: str) -> str:
        """A human line mapping the sandbox's published container ports to the host
        loopback URLs the agent can browser/web_fetch (WS-4). '' when none published."""
        try:
            from tools.shell.backend_pool import peek_backend
            backend = peek_backend(session_id)
            if backend is None:
                return ""
            ports = await backend.published_ports()
            if not ports:
                return ""
            lines = [f"  container :{c} -> http://127.0.0.1:{h}/"
                     for c, h in sorted(ports.items())]
            return ("\nIf it binds one of these ports, reach it from web_fetch/browser at:\n"
                    + "\n".join(lines))
        except Exception:
            return ""

    @staticmethod
    def _save_dir(execution_context):
        try:
            from pathlib import Path
            from agents.task.path import pm
            sid = getattr(execution_context, "session_id", None) or "shell"
            return Path(pm().get_logs_dir(sid, getattr(execution_context, "user_id", None))) / "shell"
        except Exception:
            return None

    @staticmethod
    def _audit(execution_context, executor, command: str, *, rc, duration: float,
               cwd: str, background: bool) -> None:
        """073 W1: every HOST command leaves one event (hash, not text; never output)."""
        if getattr(executor, "kind", "") != "host":
            return
        import hashlib
        from core.event_kinds import HOST_EXEC
        from core.event_log import emit
        emit(HOST_EXEC, source="shell",
             user_id=getattr(execution_context, "user_id", "") or "",
             session_id=getattr(execution_context, "session_id", "") or "",
             attrs={"cmd_sha256": hashlib.sha256(command.encode("utf-8", "replace")).hexdigest(),
                    "exit_code": rc, "duration_ms": int(duration * 1000),
                    "cwd": cwd, "background": background})

    def _task_agent(self):
        """The in-process TaskAgent (the self-wake rail's owner), or None."""
        try:
            container = self.container
        except Exception:
            container = None
        try:
            if callable(container) and not hasattr(container, "get_service"):
                container = container()
            agent = None
            if container is not None:
                if hasattr(container, "get_agent"):
                    agent = container.get_agent("task_agent")
                if agent is None and hasattr(container, "get_service"):
                    agent = container.get_service("task_agent")
            return agent
        except Exception:
            return None

    def _wake_deliverer(self, execution_context):
        """073 W4: ``deliver(text, kind)`` bound to the EXISTING self-wake rail
        (``TaskAgent.deliver_self_wake``) for this session, or None when that rail
        is off or not in this process. A note arrives as a forged ``self_wake``
        turn: data to read, no compute posture, under the re-entry budget."""
        sid = getattr(execution_context, "session_id", None) or "shell"
        uid = getattr(execution_context, "user_id", None) or ""
        try:
            from agents.task.agent.core.self_wake import effective_self_wake_enabled
            from core.runtime_paths import data_dir_or_home
            if not effective_self_wake_enabled(uid, data_dir_or_home(None)):
                return None
        except Exception:
            return None
        fn = getattr(self._task_agent(), "deliver_self_wake", None)
        if not callable(fn):
            return None

        async def _deliver(text: str, kind: str) -> bool:
            return await fn(sid, uid, text, metadata={"source": "shell_job", "kind": kind})
        return _deliver

    async def _start_job(self, executor, execution_context, sid: str, command: str, *,
                         workdir: str = "", pty: bool = False, notify=None,
                         pattern=None, deliver=None):
        import time
        job = self._registry.create(sid, command, now=time.time(),
                                    backend=getattr(executor, "kind", "docker"),
                                    pty=pty, notify=notify)
        state = self._state_for(sid, executor)
        try:
            if workdir or pty:
                await executor.start_background(command, job.id, state,
                                                workdir=workdir, pty=pty)
            else:
                await executor.start_background(command, job.id, state)
        except Exception:
            # never leave a phantom 'running' job the launch didn't actually start
            self._registry.mark(sid, job.id, "killed", now=time.time())
            raise
        self._audit(execution_context, executor, command, rc=None, duration=0.0,
                    cwd=state.cwd, background=True)
        # 073 W4: one watcher per job — the durable receipt on exit, and the
        # notify note through the self-wake rail when asked.
        try:
            from tools.shell.watch import start_watch
            start_watch(executor=executor, job=job, session_id=sid,
                        user_id=getattr(execution_context, "user_id", None),
                        registry=self._registry, notify=notify, pattern=pattern,
                        deliver=deliver)
        except Exception:
            getattr(self, "logger", logging.getLogger(__name__)).debug(
                "shell job watcher not started", exc_info=True)
        return job

    async def _sudo_password(self, execution_context, command: str, executor, *,
                             background: bool):
        """073 W5: ``(stdin_bytes, None)`` to run a host sudo command, or
        ``(None, refusal)``. The password is asked on the terminal per command and
        never stored."""
        from tools.shell import sudo as _sudo
        refuse = ("refused: sudo on the host. The agent runs as its own user by design "
                  "and never handles your password. Run the sudo step yourself, then "
                  "continue.")
        if _sudo.host_sudo_mode() != "prompt":
            return None, refuse + " (Owner: SHELL_HOST_SUDO=prompt asks you per command.)"
        if _sudo.other_escalation(command):
            return None, ("refused: only `sudo` can ask for your password here; doas, su "
                          "and pkexec stay refused on the host.")
        if background:
            return None, ("refused: a sudo command with the password prompt runs only in "
                          "the foreground (background=False, no pty).")
        from core.security.host_execution import host_seat_allowed
        if not host_seat_allowed(execution_context):
            return None, refuse
        import asyncio as _asyncio
        pw = await _asyncio.to_thread(_sudo.prompt_password, command)
        if not pw:
            return None, ("refused: sudo needs the owner's password at this terminal, and "
                          "there is no terminal or the owner declined.")
        data = pw.encode("utf-8") + b"\n"
        del pw
        return data, None

    @staticmethod
    def _bad_combination(params, notify, auto_background: bool):
        """073 W4 (cross-agent parity): the message for a bad parameter combination."""
        background = bool(params.background)
        if background and params.timeout is not None:
            return ("timeout applies only to a foreground command; a background job runs "
                    "until it exits or you kill it (use `process` wait for a bounded wait). "
                    "Drop timeout or background.")
        if params.pty and not background:
            return "pty=True needs background=True (use `process` write/submit to type into it)."
        if notify is not None and notify not in ("exit", "pattern"):
            return f"notify must be 'exit' or 'pattern', not {params.notify!r}."
        if (notify or params.notify_pattern) and not (background or auto_background):
            return "notify / notify_pattern need background=True."
        if params.notify_pattern and notify != "pattern":
            return "notify_pattern needs notify='pattern'."
        if notify == "pattern" and not (params.notify_pattern or "").strip():
            return "notify='pattern' needs notify_pattern (a regex)."
        return None

    @BaseTool.action(
        "Run a shell command (cwd/env persist across calls; background=True for "
        "servers/long jobs, with notify on exit/pattern and pty on the host; workdir "
        "for a one-call directory)",
        param_model=ShellRunParams,
    )
    async def shell_run(self, params: ShellRunParams, execution_context=None):
        if not self._allowed(execution_context):
            return ActionResult(error=(
                "shell is not available for this session: it requires the sandbox-dev "
                "compute posture (AGENT_COMPUTE_POSTURE>=1) and an owner-steered, "
                "non-delegated turn."
            ))

        command = (params.command or "").strip()
        if not command:
            return ActionResult(error="empty command")

        # 073 W2: the floor is refused on EVERY backend, before anything resolves.
        from core.security.command_guard import classify, mentions_sudo
        verdict = classify(command)
        if verdict.is_floor:
            return ActionResult(error=(
                f"refused: this command {verdict.reason}. It is on the shell guard's floor "
                "and no approval can allow it. Do not retry it in another form."
            ))

        ceiling = dev_exec_max_timeout_sec()
        auto_background = (not params.background and params.timeout is not None
                           and float(params.timeout) > ceiling)
        # 073 W4: a bad parameter combination is an error, never a silent ignore.
        notify = (params.notify or "").strip().lower() or None
        pattern = None
        bad = self._bad_combination(params, notify, auto_background)
        if bad:
            return ActionResult(error=bad)
        if notify == "pattern":
            from tools.shell.watch import compile_pattern
            try:
                pattern = compile_pattern(params.notify_pattern or "")
            except ValueError as e:
                return ActionResult(error=str(e))
        if not params.background and not auto_background:
            nudge = background_nudge(command, background=False)
            if nudge:
                return ActionResult(error=nudge)

        sid = getattr(execution_context, "session_id", None) or "shell"
        try:
            executor = await self._resolve_executor(execution_context)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"shell executor resolve failed: {e}")
            return ActionResult(error=f"shell backend unavailable: {e}")

        is_host = getattr(executor, "kind", "") == "host"
        if params.pty and not is_host:
            return ActionResult(error=(
                "pty=True runs only on the host shell (posture 3, your terminal); this "
                "turn's shell is the sandbox container. Start it without pty."
            ))

        wants_job = bool(params.background or auto_background)
        if wants_job and ((params.workdir or "").strip() or params.pty):
            import inspect
            try:
                sig = inspect.signature(executor.start_background)
                takes = "workdir" in sig.parameters and "pty" in sig.parameters
            except (TypeError, ValueError):
                takes = False
            if not takes:
                return ActionResult(error=(
                    f"the {getattr(executor, 'kind', '?')} shell backend cannot start a "
                    "background job with workdir or pty; cd in the command instead."))
        if notify == "pattern" and not hasattr(executor, "read_log_from"):
            return ActionResult(error=(
                f"notify='pattern' is not available on the {getattr(executor, 'kind', '?')} "
                "shell backend; use notify='exit'."))

        # The guard passes `rm -rf build` as "below the working directory"; the cwd
        # persists across calls, so check where that directory really is.
        import os as _os
        from core.security.command_guard import relative_delete_refusal
        _cwd = self._state_for(sid, executor).cwd
        _wd = (params.workdir or "").strip()
        if _wd:
            _cwd = _wd if _os.path.isabs(_wd) else _os.path.join(_cwd, _wd)
        _data_home = None
        if is_host:
            try:
                from core.runtime_paths import effective_data_home
                _data_home = str(effective_data_home())
            except Exception:
                _data_home = None
        refusal = relative_delete_refusal(
            command, _cwd, home=_os.path.expanduser("~") if is_host else None,
            data_home=_data_home, resolve_links=bool(is_host))
        if refusal:
            return ActionResult(error=refusal)

        run_command = command
        stdin_bytes = None
        if is_host and mentions_sudo(command):
            stdin_bytes, refusal = await self._sudo_password(
                execution_context, command, executor,
                background=bool(params.background or auto_background or params.pty))
            if refusal:
                return ActionResult(error=refusal)
            from tools.shell.sudo import rewrite_sudo
            run_command = rewrite_sudo(command)

        deliver = None
        if notify or auto_background:
            deliver = self._wake_deliverer(execution_context)
            if notify and deliver is None:
                return ActionResult(error=(
                    f"notify={notify!r} needs the self-wake rail (SELF_WAKE_ENABLED, in "
                    "this process), which is off here. Start the job without notify and "
                    "use `process` wait/poll."
                ))
            if auto_background and not notify and deliver is not None:
                notify = "exit"

        try:
            if params.background or auto_background:
                workdir = (params.workdir or "").strip()
                if workdir and is_host:
                    import os as _os
                    base = self._state_for(sid, executor).cwd
                    target = workdir if _os.path.isabs(workdir) else _os.path.join(base, workdir)
                    if not _os.path.isdir(target):
                        return ActionResult(error=f"workdir {workdir!r} does not exist")
                job = await self._start_job(executor, execution_context, sid, command,
                                            workdir=workdir, pty=bool(params.pty),
                                            notify=notify, pattern=pattern, deliver=deliver)
                ports_note = "" if is_host else await self._loopback_note(sid)
                why = (f"The requested timeout ({float(params.timeout):g}s) is above the "
                       f"foreground ceiling ({ceiling:g}s), so it runs as a background job.\n"
                       if auto_background else "")
                if notify == "exit":
                    notify_note = "\nYou get a note when it ends."
                elif notify == "pattern":
                    notify_note = ("\nYou get a note when an output line matches the pattern "
                                   "(at most one per 10s, off after 5) and when it ends.")
                elif auto_background:
                    notify_note = "\nNo note on exit (the self-wake rail is off): use `process` wait."
                else:
                    notify_note = ""
                io_note = ("It runs on a terminal: `process` write/submit type into it, close "
                           "sends Ctrl-D." if params.pty else
                           "Its stdin stays open: `process` write/submit send input, close sends EOF.")
                return ActionResult(
                    extracted_content=(
                        f"{why}Started background job `{job.id}`: {command}\n"
                        f"Use the `process` tool (wait/poll/log/kill) with this id. "
                        f"{io_note}{notify_note}{ports_note}"
                    ),
                    include_in_memory=True,
                )

            timeout = params.timeout or _DEFAULT_TIMEOUT_SEC
            timeout = max(1.0, min(float(timeout), ceiling))
            state = self._state_for(sid, executor)
            workdir = (params.workdir or "").strip()
            import time
            started = time.monotonic()
            kw = {}
            if workdir:
                kw["workdir"] = workdir
            if stdin_bytes is not None:
                kw["stdin_bytes"] = stdin_bytes  # 073 W5: the sudo password, stdin only
            try:
                clean, new_state, rc = await executor.run_foreground(
                    run_command, state, timeout=timeout, **kw)
            finally:
                stdin_bytes = None
                kw.pop("stdin_bytes", None)
            if workdir:
                # a one-call directory never moves the persisted cwd
                new_state.cwd = state.cwd
            self._states[(sid, getattr(executor, "kind", "docker"))] = new_state
            self._audit(execution_context, executor, command, rc=rc,
                        duration=time.monotonic() - started, cwd=state.cwd, background=False)
            from tools.shell.output import exit_note, shape_output
            text, _full = shape_output(clean, save_dir=lambda: self._save_dir(execution_context))
            content = text if text else "(no output)"
            if rc != 0:
                note = exit_note(rc)
                if workdir and rc == 2 and not text.strip():
                    note = note or f"workdir {workdir!r} may not exist"
                head = f"command exited {rc}" + (f" — {note}" if note else "")
                return ActionResult(error=f"{head}\n{content}")
            return ActionResult(extracted_content=content)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"shell_run failed: {e}")
            return ActionResult(error=f"shell_run failed: {e}")
