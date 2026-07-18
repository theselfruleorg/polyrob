"""polyrob run command (P3 cli/commands split; Phase 5 renderer parity).

One-shot ``polyrob run <task>`` shares the renderer with the REPL: Rich when stdout
is a TTY (and not ``NO_COLOR`` / ``--plain``), Plain otherwise.  The whole-phase
``/dev/null`` redirect is gone — only the narrow bootstrap window is suppressed
(``_bootstrap.suppress_bootstrap_output``); the renderer owns stdout for the run
and errors surface.
"""
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import click
from core.runtime_paths import data_dir_or_home


def _resolve_tool_list(
    tools: Optional[str], toolset: Optional[str],
    *, user_id: Optional[str] = None, home_dir=None,
) -> tuple[list[str], list[str]]:
    """Resolve the final CLI tool list from --tools / --toolset.

    Precedence: ``--tools`` (explicit comma list) > ``--toolset`` (named set) >
    a ``session.toolset`` preference (owner-UX P1 T5, only consulted when
    ``user_id`` is given) > default. The final list is always pruned through
    ``cli_unavailable_tools`` so the agent is never advertised tools the CLI
    container can't register.

    Returns ``(tool_list, notes)`` where ``notes`` are human-readable warning
    lines (stderr) about pruned/unavailable tools. Pure + side-effect-free (the
    optional pref read is the only I/O) so it is directly unit-testable (the
    caller does the echoing). ``user_id``/``home_dir`` default to None, so every
    pre-existing positional call (no pref file involved) stays byte-identical.
    """
    from agents.task.tool_defaults import cli_default_tools, resolve_toolset
    from core.bootstrap import cli_unavailable_tools

    notes: list[str] = []

    if tools:
        # Explicit list wins; still prune unavailable tools.
        tool_list = tools.split(",")
        missing = cli_unavailable_tools(tool_list)
        if missing:
            notes.append(
                f"note: tool(s) {', '.join(missing)} are not available in the CLI "
                f"(they need the server container); continuing without them."
            )
            tool_list = [t for t in tool_list if t not in set(missing)]
    elif toolset:
        # Named toolset, pruned through cli_unavailable_tools.
        resolved = resolve_toolset(toolset)
        unavail = set(cli_unavailable_tools(resolved))
        if unavail:
            notes.append(
                f"note: tool(s) {', '.join(sorted(unavail))} from toolset '{toolset}' are not "
                f"available in the CLI (they need the server container); continuing without them."
            )
        tool_list = [t for t in resolved if t not in unavail]
    else:
        # owner-UX P1 T5: neither --tools nor --toolset given for THIS run — a
        # "session.toolset" pref may override the default toolset NAME. Only
        # takes effect when a pref is actually ON DISK (resolve_with_source's
        # "pref" source); otherwise falls through to cli_default_tools()
        # unchanged (byte-identical legacy, including its own env read + pruning).
        tool_list = None
        if user_id:
            try:
                from core.prefs import resolve_with_source
                env_toolset = os.environ.get("POLYROB_AGENT_TOOLSET", "").strip() or None
                pref_toolset, source = resolve_with_source(
                    "session.toolset", user_id, data_dir_or_home(home_dir),
                    env_value=env_toolset, default=None,
                )
                if source == "pref" and pref_toolset:
                    resolved = resolve_toolset(pref_toolset)
                    unavail = set(cli_unavailable_tools(resolved))
                    tool_list = [t for t in resolved if t not in unavail]
            except Exception:
                tool_list = None
        if tool_list is None:
            tool_list = cli_default_tools()

    return tool_list, notes


@click.command()
@click.argument("task", required=False)
@click.option("--resume", "resume_id", default=None, metavar="SESSION_ID",
              help="Resume an existing session by id (continue it) instead of starting a new task.")
@click.option("--model", "-m", default=None, help="Model name (e.g. gemini-2.5-flash, gpt-5)")
@click.option("--provider", "-p", default=None, help="Provider (openrouter, anthropic, openai, gemini, nvidia; DeepSeek via openrouter + deepseek/deepseek-chat)")
@click.option("--tools", "-t", default=None, help="Comma-separated tool list (e.g. browser,mcp,filesystem). Takes precedence over --toolset.")
@click.option("--toolset", default=None, help="Named toolset (minimal/default/research/coding/development/browser/full/safe). Ignored when --tools is given.")
@click.option("--max-steps", default=50, type=int, help="Maximum steps (default: 50)")
@click.option("--plain", is_flag=True, help="Force plain, line-oriented output (no ANSI / panels)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def run(
    task: Optional[str],
    resume_id: Optional[str],
    model: Optional[str],
    provider: Optional[str],
    tools: Optional[str],
    toolset: Optional[str],
    max_steps: int,
    plain: bool,
    verbose: bool,
):
    """Run a task session locally, streaming output to the terminal.

    Provide a TASK to start a new session, or --resume SESSION_ID to continue an
    existing one (exactly one of the two).
    """
    if bool(task) == bool(resume_id):
        raise click.UsageError("provide either a TASK or --resume SESSION_ID (exactly one).")
    # The banner announces the resolved model — no extra echo needed.
    asyncio.run(_run_session(task, model, provider, tools, toolset, max_steps, plain, verbose,
                             resume_id=resume_id))


async def _run_session(
    task: Optional[str],
    model: Optional[str],
    provider: Optional[str],
    tools: Optional[str],
    toolset: Optional[str],
    max_steps: int,
    plain: bool,
    verbose: bool,
    resume_id: Optional[str] = None,
):
    """Create and execute a task session, rendering the feed to stdout."""
    import logging as _logging

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    from cli.commands._bootstrap import suppress_bootstrap_output

    setup_project_path()
    setup_sqlite_compat()

    # Pre-flight: load env layers first (so file-based keys are visible), then ensure a
    # usable provider key — onboard inline on a TTY, else print the canonical message and
    # exit. build_cli_container re-loads env idempotently (override=False).
    from core.bootstrap import load_env
    from cli.keys import preflight_or_onboard
    load_env(local_mode=True)
    if not preflight_or_onboard(interactive=True):
        sys.exit(1)

    log_level = "DEBUG" if verbose else "ERROR"
    if not verbose:
        _logging.disable(_logging.CRITICAL)
        os.environ["GRPC_VERBOSITY"] = "ERROR"
        os.environ["GLOG_minloglevel"] = "3"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        os.environ["TQDM_DISABLE"] = "1"

    # POLYROB_PLAIN env mirrors the REPL's plain toggle; --plain takes precedence.
    from core.env import bool_env as _bool_env
    plain = plain or _bool_env("POLYROB_PLAIN", False)  # SSOT falsey/truthy set (incl. "on") — P4

    click.echo(click.style("starting…", dim=True))

    # Narrow bootstrap-only suppression (proposal §9): silence MCP config / gRPC
    # bootstrap prints, then hand stdout back to the renderer.  Errors after this
    # point surface instead of vanishing into /dev/null.
    # F6: a failed container build must surface a clean error + non-zero exit,
    # not a raw traceback escaping through asyncio.run.
    try:
        with suppress_bootstrap_output():
            container = await build_cli_container(log_level=log_level)
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to start: {e}")
        sys.exit(1)
    if not verbose:
        # Lift the bootstrap-wide logging.disable(CRITICAL) now that startup is
        # done; per-sink handler levels (console=ERROR) keep the terminal quiet.
        # Mirrors chat.py.
        _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "TaskAgent not available in container")
        sys.exit(1)

    # Resolve model/provider defaults
    from cli.config_store import resolve_provider_model
    resolved_provider, resolved_model = resolve_provider_model(provider, model)
    if resolved_model is None:
        # Single source of truth for provider defaults (matches `rob model list`).
        from modules.llm.llm_client_registry import get_default_model as _registry_default
        resolved_model = _registry_default(resolved_provider)

    # owner-UX P1 T5: resolved early (moved up from below _resolve_tool_list) so
    # the "session.toolset" pref lookup below can be tenant-scoped. The rest of
    # this function is unchanged from here down.
    user_id = container.get_service("identity").resolve()
    _data_home = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))

    # Parse tools
    # Precedence: --tools (explicit comma list) > --toolset (named set) >
    # session.toolset pref > default.
    # B6: the CLI container deliberately skips heavy browser init (build_cli_container),
    # so a default that advertises `browser` only yields "No BrowserManager" errors.
    # Match the REPL default (filesystem, task; fast startup, no browser). Browser is
    # still opt-in via `--tools browser` or `--toolset browser` for anyone who wires
    # a BrowserManager.
    tool_list, notes = _resolve_tool_list(tools, toolset, user_id=user_id, home_dir=_data_home)
    for note in notes:
        click.echo(note, err=True)

    # C1/A3: expand @file/@folder/@diff/@url references in the task text (opt-in).
    # Confined to the invocation CWD so a reference can't read outside the working
    # dir or hit a private host; fails soft (oversized/unsafe refs become notes).
    from agents.task.constants import AutonomyConfig
    if task and AutonomyConfig.context_references_enabled():
        try:
            from agents.task.agent.messages.context_references import (
                preprocess_context_references,
            )
            expanded = preprocess_context_references(
                task, root=os.getcwd(), confine_to_root=True
            )
            if expanded != task:
                click.echo(click.style("expanded context references in task input", dim=True))
                task = expanded
        except Exception as e:
            click.echo(click.style(f"context-ref expansion skipped: {e}", dim=True))

    # Build session request
    request = {
        "task": task,
        "model": resolved_model,
        "provider": resolved_provider,
        "tools": tool_list,
        "max_steps": max_steps,
        "temperature": 0.0,
        "use_vision": True,
    }

    # Select the renderer (Rich on TTY, Plain otherwise).  one_shot=True so the
    # completion panel shows the final result (it IS the summary here).
    # Capture the real stdout *before* the bootstrap window so the renderer
    # writes to the terminal even while agent noise is suppressed.
    _cli_out = sys.stdout
    from agents.task.telemetry.service import ProductTelemetry
    from cli.ui import select_renderer
    from cli.ui.events import SessionDone
    from cli.ui.events import normalize as _normalize_event
    from cli.ui.state import SessionState

    _ui_state = SessionState()
    _renderer = select_renderer(_ui_state, plain=plain, stream=_cli_out, one_shot=True)
    # --verbose drives the renderer's trace layer too (full step blocks live),
    # not just the log level — same contract as the REPL's /verbose toggle.
    _renderer.verbose = verbose

    # Suppress noise during session creation (browser/tool init prints).  The
    # restore is guaranteed even if create_session raises (e.g. session-limit),
    # so the error surfaces instead of being swallowed by the redirect.
    if resume_id:
        # Resume: rehydrate the existing session's orchestrator from disk (the same
        # machinery a warm-but-evicted session uses) instead of creating a new one;
        # run_session then transitions its status (suspended/completed → resumed →
        # running) and continues it.
        from agents.task.path import pm as _pm
        session_id = _pm().clean_session_id(resume_id)
        info = task_agent.session_manager.get_session_info(session_id)
        if not info:
            click.echo(click.style("[polyrob] ERROR: ", fg="red")
                       + f"Session {resume_id} not found (try `polyrob session list`)")
            sys.exit(1)
        orch = task_agent.get_orchestrator(session_id)
        if orch is None:
            with suppress_bootstrap_output():
                orch = await task_agent._recreate_orchestrator(session_id, info)
        if orch is None:
            click.echo(click.style("[polyrob] ERROR: ", fg="red")
                       + f"Could not resume session {resume_id} (missing session metadata)")
            sys.exit(1)
        # Use the session's own model/task for the banner + turn label.
        resolved_model = info.get("model") or resolved_model
        resolved_provider = info.get("provider") or resolved_provider
        task = info.get("task") or "(resumed session)"
    else:
        try:
            with suppress_bootstrap_output():
                session_info = await task_agent.create_session(
                    user_id=user_id,
                    request=request,
                    skip_credit_check=True,
                )
            session_id = session_info["id"]
        except Exception as e:
            # F1/F8: render the actionable block for a session-limit error (shared
            # with the REPL), the raw message otherwise.
            from cli.commands._errors import echo_create_session_error
            echo_create_session_error(e, user_id)
            sys.exit(1)

    # First-run banner (proposal §9): one compact panel — version, model/provider,
    # tools, short session id, configured-provider key NAMES (never values).
    try:
        from cli.polyrob import VERSION as _ROB_VERSION
    except Exception:
        from core.version import get_version
        _ROB_VERSION = get_version()
    try:
        from core.config import AgentConfig

        from cli.ui.banner import print_banner
        from core.instance import FRAMEWORK_NAME, resolve_instance_id
        print_banner(
            _renderer,
            version=_ROB_VERSION,
            model=resolved_model,
            provider=resolved_provider,
            tool_ids=tool_list,
            session_id=session_id,
            config=AgentConfig(),
            framework=FRAMEWORK_NAME,
            instance_id=resolve_instance_id(),
            user_id=user_id,
        )
    except Exception:
        # Banner is cosmetic; never block the run on it.
        pass

    # Resolve the session dir so we can poll llm_usage for live tokens/cost (the
    # live path writes usage to disk, not the push feed — §0 amend. 1).  poll_usage
    # is fired from inside the feed callback the moment SessionDone arrives, BEFORE
    # the event reaches the renderer, so the completion panel shows real totals.
    session_dir: Optional[Path] = None
    try:
        from agents.task.path import pm
        session_dir = pm().get_session_root(session_id, user_id)
    except Exception:
        session_dir = None

    # Stash the agent's actual final-result text when SessionDone arrives.
    # list used as a mutable cell so the closure can write to it.
    _final_result: list[str] = []

    def _feed_callback(_session_id: str, event_dict: dict) -> None:
        event = _normalize_event(event_dict)
        _ui_state.update(event)
        if isinstance(event, SessionDone):
            if event.final_result:
                _final_result[:] = [event.final_result]
            if session_dir is not None:
                # Poll authoritative tokens/cost before the renderer paints the
                # completion panel (which reads state.tokens_total / cost_estimate).
                try:
                    _ui_state.poll_usage(session_dir)
                except Exception:
                    pass
        _renderer.on_event(event)

    ProductTelemetry._on_feed_entry = _feed_callback

    # Phase 3: wire the orchestrator-level stream callback before run_session
    # creates the agent (create_agent registers it on the agent's hitl_manager —
    # the webview seam).  on_stream_delta routes into the renderer's response box
    # (Rich) or buffer (Plain); on_turn_end prints/finalizes the answer once.
    try:
        orchestrator = task_agent.get_orchestrator(session_id)
        if orchestrator is not None:
            from cli.ui.streaming import make_stream_callback
            orchestrator._on_stream_chunk = make_stream_callback(
                _renderer,
                main_agent_id=lambda: _ui_state.main_agent_id,
            )
    except Exception as _stream_err:
        click.echo(click.style(f"streaming display unavailable: {_stream_err}", dim=True))

    # Stamp the init-chosen persona into the orchestrator so run_session /
    # create_agent builds the agent with the correct <identity> block — mirrors
    # the REPL path in chat.py.  Fail-open: a bad persona never blocks the run.
    try:
        from cli.persona import resolve_cli_persona
        _p = resolve_cli_persona(user_id=user_id, home_dir=_data_home)
        if _p and orchestrator is not None:
            orchestrator._persona_block = _p
    except Exception:
        pass  # fail-open

    # Install signal handler for clean cancellation.  Capture the running loop
    # here, where a loop is guaranteed to exist; get_running_loop() inside the
    # signal handler itself can raise (handlers fire on the main thread outside
    # the coroutine scheduler), so capturing beforehand is safest.
    loop = asyncio.get_running_loop()
    cancelled = False

    def _signal_handler(sig, frame):
        nonlocal cancelled
        if cancelled:
            click.echo("\n" + click.style("[polyrob] ", fg="red") + "Force exit")
            sys.exit(1)
        cancelled = True
        click.echo("\n" + click.style("[polyrob] ", fg="yellow") + "Cancelling (Ctrl+C again to force)...")
        loop.call_soon_threadsafe(
            lambda: loop.create_task(_cancel_session(task_agent, user_id, session_id))
        )

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Turn lifecycle parity with the REPL: on_turn_start before the run resets
    # per-turn streaming state; on_turn_end after the run prints/finalizes the
    # answer exactly once (the completion panel from SessionDone shows the
    # summary; the answer block is the canonical assistant message).
    _renderer.on_turn_start(task)

    try:
        result = await task_agent.run_session(user_id=user_id, session_id=session_id)
        # Final safety poll (idempotent — only reads usage files not yet seen)
        # in case SessionDone didn't arrive on the feed.
        if session_dir is not None:
            try:
                _ui_state.poll_usage(session_dir)
            except Exception:
                pass
        # Prefer the agent's real final-result text (stashed from the
        # session_completion feed event) over run_session's generic return
        # string ("Session completed successfully").
        answer = _final_result[0] if _final_result else (result or "")
        _renderer.on_turn_end(answer)
        # No trailing "Done." — the turn summary line is the completion signal.
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + str(e))
        sys.exit(1)
    finally:
        ProductTelemetry._on_feed_entry = None


async def _cancel_session(task_agent, user_id: str, session_id: str):
    """Cancel the running session."""
    try:
        await task_agent.cancel_session(user_id=user_id, session_id=session_id)
    except Exception:
        pass


# --- Session subgroup ---
