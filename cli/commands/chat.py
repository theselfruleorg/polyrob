"""rob interactive REPL (R6 + Phase 2).

`rob` with no subcommand opens a conversation loop driving Conversation.respond()
once per line. The loop is factored behind an injectable `read_line` seam so it
can be unit-tested without a TTY.

Phase 2: the renderer owns stdout (Rich when TTY, Plain otherwise); input is a
prompt_toolkit PromptSession with a live bottom status toolbar, FileHistory, and
Meta/Enter multi-line; the whole-loop /dev/null redirect is replaced by a narrow
bootstrap-only suppression.
"""
import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import click

from cli.commands._bootstrap import suppress_bootstrap_output as _suppress_bootstrap_output
from cli.persona import resolve_cli_persona
from cli.ui.commands import ReplExit

# Back-compat alias: existing tests import resolve_repl_persona from this module.
resolve_repl_persona = resolve_cli_persona


async def _conversation_loop(
    convo,
    container,
    read_line=None,
    renderer=None,
    on_turn_complete=None,
    slash_dispatch=None,
):
    """Drive Conversation.respond() once per input line.

    read_line: zero-arg awaitable returning the next line, raising EOFError when
    the input is exhausted. Defaults to a threaded prompt read.

    renderer: optional Renderer instance.  When provided, answers are printed
    via renderer.on_turn_start() / renderer.on_turn_end() rather than with a
    bare click.echo(), so the renderer becomes the single place the answer is
    printed.

    on_turn_complete: optional zero-arg callback fired after respond() returns
    and BEFORE renderer.on_turn_end (used to poll the session's llm_usage dir
    into SessionState so the turn summary + toolbar see live tokens/cost).
    Best-effort; never raises into the loop.

    slash_dispatch: optional ``async (line) -> bool`` dispatcher for slash
    commands.  Returns True when the line was a slash command (don't treat it
    as a turn); raises ``ReplExit`` for ``/exit``.  Defaults to the built-in
    registry dispatcher bound to this ``convo``/``container``.
    """
    if read_line is None:
        # Piped/CI stdin: no visible prompt — the renderer echoes each turn as
        # '› {text}', and input()'s prompt would glue a stray '› ' onto the
        # next output line of the transcript.
        _prompt = "› " if sys.stdin.isatty() else ""

        async def read_line():  # noqa: F811
            return await asyncio.to_thread(input, _prompt)

    if slash_dispatch is None:
        slash_dispatch = _make_default_slash_dispatch(convo, container, renderer)

    _pending_redirect: Optional[str] = None  # T16: redirect text queued after Ctrl-C
    while True:
        # T16: a redirect captured during the previous in-turn Ctrl-C becomes the next
        # line without a new read_line call (avoids a stale prompt / double-append).
        if _pending_redirect is not None:
            line = _pending_redirect
            _pending_redirect = None
        else:
            try:
                line = (await read_line()).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                # Ctrl-C at the prompt: drop the current line, keep the loop alive.
                click.echo("")
                continue

        if not line:
            continue

        if line.startswith("/"):
            try:
                handled = await slash_dispatch(line)
            except ReplExit:
                break
            if handled:
                continue
            # not a recognized slash → fall through and treat as a turn

        # C1: expand @file/@folder/@diff/@url references in real user turns (opt-in).
        # Confined to CWD; fails soft — an expansion error leaves the line unchanged.
        try:
            from agents.task.constants import AutonomyConfig
            if AutonomyConfig.context_references_enabled():
                from agents.task.agent.messages.context_references import (
                    preprocess_context_references,
                )
                line = preprocess_context_references(line, root=os.getcwd(), confine_to_root=True)
        except Exception:
            pass  # fail-soft: leave line unchanged

        # Turn boundary drives the TurnLifecycle (begin on submit → end on
        # deliver/error/cancel), guarded by try/finally + a token so end_turn fires
        # exactly once on every exit (including the redirect/abort `continue`s) and a
        # stale cancelled turn can't settle a later one.
        from cli.ui.lifecycle import TurnOutcome
        from cli.ui.persistent_loop import lifecycle_of, sync_status

        _lifecycle = lifecycle_of(renderer)
        _token = _lifecycle.begin_turn() if _lifecycle is not None else 0
        sync_status(renderer)
        _outcome = TurnOutcome.OK
        try:
            if renderer is not None:
                renderer.on_turn_start(line)

            try:
                from core.interactive_gate import interactive_turn
                with interactive_turn():
                    answer = await convo.respond(line)
            except KeyboardInterrupt:
                # T16: interrupt-and-redirect — when INTERRUPT_REDIRECT is ON, prompt the
                # user for a redirect instruction instead of silently dropping the turn.
                #
                # Limitation (documented): convo.respond() is an async Task; the
                # KeyboardInterrupt unwinds it (the current turn is already cancelled by the
                # time we reach this handler). We cannot inject guidance MID-FLIGHT into the
                # running agent step — this is NEXT-TURN injection, identical to how
                # /toolset and /persona work. The redirect becomes the next turn's input.
                _outcome = TurnOutcome.CANCELLED
                try:
                    from agents.task.constants import AutonomyConfig
                    _redirect_enabled = AutonomyConfig.interrupt_redirect_enabled()
                except Exception:
                    _redirect_enabled = False

                if _redirect_enabled:
                    _redirect_text = None
                    try:
                        click.echo("\n" + click.style("[polyrob] ", fg="yellow") + "Turn interrupted.")
                        click.echo(click.style("↪ redirect (blank = cancel): ", dim=True), nl=False)
                        _raw = (await read_line()).strip()
                        if _raw:
                            _redirect_text = _raw
                    except Exception:
                        # Fail-open: any error in the redirect prompt falls through to abort.
                        _redirect_text = None

                    if _redirect_text:
                        # Queue for the next loop iteration — avoids an extra read_line call
                        # and keeps the turn boundary clean.
                        _pending_redirect = _redirect_text
                        click.echo(click.style("[polyrob] ", fg="cyan") + "Redirecting…")
                        continue
                    # Blank or error → abort (fall through; already printed "Turn interrupted.")
                    continue

                # Flag OFF: existing abort behavior.
                click.echo("\n" + click.style("[polyrob] ", fg="yellow") + "Turn interrupted.")
                continue
            except Exception as e:
                # Dialog layer: a failed turn is an error the user must see — never
                # a raw traceback that kills the REPL (e.g. InterruptedError after
                # a fatal LLM halt).  Render it, close the turn, keep the loop.
                _outcome = TurnOutcome.ERROR
                if renderer is not None:
                    from cli.ui.events import ErrorEvent
                    renderer.on_event(
                        ErrorEvent(error_message=str(e), error_type=type(e).__name__)
                    )
                    renderer.on_turn_end("")
                else:
                    click.echo(click.style("error: ", fg="red") + f"{type(e).__name__}: {e}")
                continue

            # Poll live usage BEFORE on_turn_end: the turn summary line reads the
            # token/cost deltas from SessionState, and the live path only writes
            # usage to disk (llm_usage files), never to the push feed.
            if on_turn_complete is not None:
                try:
                    on_turn_complete()
                except Exception:
                    pass

            if renderer is not None:
                renderer.on_turn_end(answer or "")
            elif answer:
                click.echo(answer)
        finally:
            if _lifecycle is not None:
                _lifecycle.end_turn(_token, _outcome)
            sync_status(renderer)


def _make_default_slash_dispatch(convo, container, renderer, *, state=None, session_id="", user_id="local", task_agent=None, orchestrator=None):
    """Build the default registry-backed slash dispatcher for the REPL.

    Returns an ``async (line) -> bool`` that builds a fresh ``CommandContext``
    per invocation and routes through ``default_registry().dispatch``.
    """
    from cli.ui.commands import CommandContext, default_registry

    registry = default_registry()

    async def _dispatch(line: str) -> bool:
        ctx = CommandContext(
            renderer=renderer,
            state=state,
            conversation=convo,
            container=container,
            task_agent=task_agent,
            orchestrator=orchestrator,
            session_id=session_id,
            user_id=user_id,
            registry=registry,
        )
        return await registry.dispatch(line, ctx)

    return _dispatch


async def _run_persistent_app(
    convo,
    state,
    renderer,
    slash_dispatch,
    poll_usage,
    *,
    completer=None,
):
    """Run the persistent bottom-anchored Application loop (POLYROB_PERSISTENT_INPUT).

    One long-lived Application; each turn runs as a background task while the
    input + status region stay pinned + live. Content prints above via
    patch_stdout's run_in_terminal. The turn control flow is the unit-tested
    ``persistent_loop`` module; this is the thin Application glue.

    NOTE: this is the DEFAULT REPL path on a TTY (POLYROB_PERSISTENT_INPUT default ON;
    set it to 0/off to force the legacy ephemeral prompt). The print routing
    (run_in_terminal above the region) is the key thing to verify on a real terminal.
    """
    from prompt_toolkit.patch_stdout import patch_stdout

    from cli.ui.app import build_app
    from cli.ui.persistent_loop import TurnController, run_turn

    holder: dict = {}

    def _on_submit(text: str) -> None:
        ctrl = holder.get("ctrl")
        if ctrl is not None:
            ctrl.submit(text)

    def _on_interrupt() -> None:
        ctrl = holder.get("ctrl")
        if ctrl is not None:
            ctrl.interrupt()

    app, _buf = build_app(
        state, on_submit=_on_submit, on_interrupt=_on_interrupt, completer=completer
    )

    # The persistent box repaints a live spinner in its status bar, so the
    # renderer should suppress the static "working…" turn-start line.
    try:
        renderer.live_status_bar = True
    except Exception:
        pass

    def _factory(line: str):
        return run_turn(
            convo, line, renderer,
            on_turn_complete=poll_usage,
            slash_dispatch=slash_dispatch,
            request_exit=app.exit,
        )

    holder["ctrl"] = TurnController(
        run_coro_factory=_factory,
        schedule=lambda coro: app.create_background_task(coro),
    )
    state._app = app  # the feed callback invalidates this to repaint live
    # Start at the lifecycle's calm idle word ("ready", no spinner) instead of the
    # bootstrap "starting" — derived from the single source of truth, not a literal.
    state.status = state.lifecycle.status_word()
    try:
        # raw=True: the renderer prints colored Rich output (ANSI). The default
        # StdoutProxy ESCAPES control chars (output.write), so the agent's
        # speaker line / markdown leaked as literal "?[1;32m…" text above the
        # pinned box. raw mode uses output.write_raw → ANSI passes through and the
        # terminal interprets it.
        with patch_stdout(raw=True):
            await app.run_async()
    except EOFError:
        pass  # Ctrl-D exit
    finally:
        state._app = None


async def _start_repl_agent(task_agent, orchestrator, request, session_id):
    """Obtain the REPL's executor Agent, creating it if needed.

    ``create_session`` only builds the orchestrator, so the REPL builds the agent
    itself (mirrors ``run_session``'s first-task path). Returns ``(agent, preexisted)``.
    On a build failure — the most likely "REPL won't start" cause (an invalid
    persisted model, a provider client that won't build, a network error resolving
    the LLM) — it prints a clean ``[polyrob] ERROR`` and returns ``(None, False)`` so
    the caller aborts without a raw traceback escaping ``asyncio.run``.
    """
    agent_id = f"executor_{session_id}"
    agent = orchestrator.agents.get(agent_id)
    if agent is not None:
        return agent, True
    try:
        llm = await task_agent._get_llm_for_request(request)
        # The Agent requires a non-empty task at construction; in REPL mode the real
        # input arrives per-turn via Conversation.respond -> set_turn_input, so seed a
        # neutral placeholder. create_agent registers the stream callback set above.
        agent = await orchestrator.create_agent(
            task="Interactive conversation session.",
            llm=llm,
            agent_name="executor",
            use_vision=False,
            max_actions_per_step=10,
        )
        return agent, False
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to start agent: {e}")
        return None, False


async def _repl_main(plain: bool = False, lifecycle_ref: Optional[dict] = None,
                     *, model: Optional[str] = None, provider: Optional[str] = None,
                     toolset: Optional[str] = None):
    """Build the container, create an interactive session, run the loop.

    plain: force the plain renderer (the ``--plain`` flag).  ``POLYROB_PLAIN`` and
    the centralized fallback detection (non-TTY / ``NO_COLOR`` / ``TERM=dumb``)
    are still honoured via ``select_renderer``.

    lifecycle_ref: optional mutable dict the coroutine populates with
    ``{"task_agent", "user_id", "session_id"}`` as soon as each is known.  The
    synchronous SIGINT fallback in ``run_repl`` reads it to flip the session to
    a terminal status when a Ctrl-C escapes ``asyncio.run`` before the async
    cancel in ``finally`` could run (F2 — the session-leak fix).
    """
    if lifecycle_ref is None:
        lifecycle_ref = {}
    import logging as _logging
    from core.bootstrap import build_cli_container, load_env, setup_project_path, setup_sqlite_compat

    setup_project_path()
    setup_sqlite_compat()

    # Load env layers up front (./.polyrob/.env, ~/.polyrob/.env, root .env, config/.env.*)
    # so the API-key presence check below sees file-based keys, not just process
    # env. build_cli_container loads them again (idempotent with override=False).
    load_env(local_mode=True)

    # Quiet logging / library noise for an interactive prompt.
    _logging.disable(_logging.CRITICAL)
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    os.environ.setdefault("GLOG_minloglevel", "3")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TQDM_DISABLE", "1")

    # Project session storage.
    (Path.cwd() / ".polyrob" / "sessions").mkdir(parents=True, exist_ok=True)

    # Graceful onboarding: if NO usable provider key is present after env-load (+ backfill),
    # onboard INLINE (OpenRouter-first wizard) on a TTY and fall through into the REPL in the
    # same process; on a non-TTY / declined, print the canonical message and return. Drives
    # off the initializable oracle so a deepseek-only env onboards instead of crashing.
    from cli.keys import first_run_no_config, preflight_or_onboard, should_warn_no_key
    if should_warn_no_key() and first_run_no_config():
        click.echo("👋 Looks like your first run — let's set up a provider key "
                   "(OpenRouter recommended).")
    if not preflight_or_onboard(interactive=True):
        return

    # Bug E: a TRANSIENT 'starting…' notice — erased once the container is built
    # so it doesn't linger at the top of the transcript (Claude-Code clean head).
    # Capture the REAL stdout now, before the bootstrap suppression swaps it.
    from cli.ui.bootstrap_notice import clear_start_notice, show_start_notice
    _start_out = sys.stdout
    _start_transient = show_start_notice(_start_out)

    # Narrow bootstrap-only suppression (proposal §9): silence MCP config /
    # gRPC bootstrap prints, then hand stdout back to the renderer. Errors after
    # this point surface instead of vanishing into /dev/null.
    # F6: a failed container build must surface a clean error + return, not a
    # raw traceback escaping through asyncio.run.
    try:
        with _suppress_bootstrap_output():
            container = await build_cli_container(log_level="ERROR")
    except Exception as e:
        clear_start_notice(_start_out, _start_transient)
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to start: {e}")
        return
    clear_start_notice(_start_out, _start_transient)
    _logging.disable(_logging.ERROR)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "TaskAgent not available in container")
        return

    user_id = container.get_service("identity").resolve()

    # Publish task_agent/user_id to the lifecycle ref now so the synchronous
    # SIGINT fallback in run_repl can reach the session_manager even if a Ctrl-C
    # lands mid-session (F2 belt-and-braces).
    lifecycle_ref["task_agent"] = task_agent
    lifecycle_ref["user_id"] = user_id

    # Resolve model/provider the same way `polyrob run` does.
    from cli.config_store import resolve_provider_model
    # Honor the launch flags (parity with `polyrob run`); fall back to the resolver.
    provider, model = resolve_provider_model(provider, model)
    if model is None:
        from modules.llm.llm_client_registry import get_default_model as _registry_default
        model = _registry_default(provider)

    # Install the feed callback via the new renderer pipeline.
    # Rich renderer when stdout is a real TTY (and not NO_COLOR); plain otherwise.
    from agents.task.telemetry.service import ProductTelemetry
    from cli.ui import select_renderer
    from cli.ui.events import normalize as _normalize_event
    from cli.ui.state import SessionState

    plain = plain or os.environ.get("POLYROB_PLAIN", "").lower() in ("1", "true", "yes")
    _cli_out = sys.stdout
    _ui_state = SessionState()
    # live_allowed=False: the REPL runs under prompt_toolkit's patch_stdout, where
    # Rich Live regions corrupt the cursor (stacked "thinking" lines + stray `\`).
    # The pinned bottom_toolbar is the in-flight indicator instead.
    _renderer = select_renderer(
        _ui_state, plain=plain, stream=_cli_out, live_allowed=False
    )

    # The feed sink is a GLOBAL class attribute fired for EVERY session's events
    # (it ignores the producer). A background autonomy turn (cron/goal/self-wake)
    # or a foreign session would otherwise leak into the user's status AND
    # transcript. Scope the sink to the REPL's OWN session; the id is published
    # here once it exists (fail-open until then / when an event carries no id).
    _repl_sid: dict = {"id": None}

    def _is_foreground_session(sid: str) -> bool:
        """True when *sid* is the REPL's interactive session (or unknown → fail-open)."""
        repl_sid = _repl_sid.get("id")
        if not repl_sid or not sid:
            return True  # fail-open: don't accidentally mute the user's own events
        try:
            from agents.task.path import pm
            return pm().clean_session_id(sid) == pm().clean_session_id(repl_sid)
        except Exception:
            return sid == repl_sid

    # Tracks in-flight BACKGROUND session ids (cron/goal/self-wake on a foreign
    # session) so the muted ``⟲ autonomy`` indicator reflects real activity and the
    # counter can't leak on a duplicate start. A set (not a bare counter) is
    # idempotent under repeated SessionStart and a missing SessionDone self-heals on
    # the next start of the same id.
    _bg_sessions: set = set()

    # Session dir for LIVE token/cost counting: per-call usage is written to disk
    # (capture_llm_usage skips the push feed to avoid double-counting), so the bar's
    # tokens/cost only tick up when poll_usage re-reads the NEW files. Polling per
    # feed event (below) makes them update live during the turn, not just at its end.
    # Published once the session id is known (mirrors _repl_sid).
    _usage_dir: dict = {"path": None}
    _agent_ref: dict = {"agent": None}  # published post-agent-creation for the ctx% poll

    def _poll_usage_live() -> None:
        """Re-read new llm_usage files so the bar's tokens/cost update mid-turn, and
        refresh the live context-window % from the agent's message_manager.

        Idempotent (poll_usage tracks already-seen files → no double-count with the
        end-of-turn poll) and fail-open (poll_usage/poll never raise into the loop)."""
        _dir = _usage_dir.get("path")
        if _dir is not None:
            _ui_state.poll_usage(_dir)
        # Feed the `ctx N%` toolbar segment live — previously only /status polled it,
        # so the segment stayed at 0% during a turn (then froze on a manual /status).
        _ag = _agent_ref.get("agent")
        if _ag is not None:
            _ui_state.poll(_ag)

    def _route_background(session_id: str, event) -> None:
        """Light/extinguish the autonomy lane from a foreign session's start/done."""
        from cli.ui.events import SessionDone, SessionStart

        lc = getattr(_ui_state, "lifecycle", None)
        if lc is None:
            return
        if isinstance(event, SessionStart) and session_id not in _bg_sessions:
            _bg_sessions.add(session_id)
            lc.begin_background()
        elif isinstance(event, SessionDone) and session_id in _bg_sessions:
            _bg_sessions.discard(session_id)
            lc.end_background()
        else:
            return
        _app = getattr(_ui_state, "_app", None)
        if _app is not None:
            try:
                _app.invalidate()
            except Exception:
                pass

    def _feed_callback(session_id: str, event_dict: dict) -> None:
        event = _normalize_event(event_dict)
        # Foreign / background-session events must not drive the foreground status,
        # clock, or transcript — route them to the muted autonomy lane instead.
        if not _is_foreground_session(session_id):
            _route_background(session_id, event)
            return
        _ui_state.update(event)
        _renderer.on_event(event)
        # Live token/cost: pick up any usage file written by this LLM call BEFORE
        # the repaint, so the bar's tokens/cost tick up mid-turn (not only at end).
        _poll_usage_live()
        # D5: repaint the persistent bottom region live during the turn (loop-affine
        # — the feed callback runs on the event loop thread). No-op classic path.
        _app = getattr(_ui_state, "_app", None)
        if _app is not None:
            try:
                _app.invalidate()
            except Exception:
                pass

    ProductTelemetry._on_feed_entry = _feed_callback

    # Compute the session tool list dynamically. Cronjob/goal tools are registered
    # as container SERVICES; they only become callable when their tool_id is in the
    # session's "tools" list (→ load_tools_from_container). Add the enabled autonomy
    # tools when the local profile is on AND the service is actually registered.
    from agents.task.constants import local_mode_enabled
    from agents.task.tool_defaults import cli_default_tools
    if toolset:
        from agents.task.tool_defaults import resolve_toolset
        repl_tools = list(resolve_toolset(toolset))
    else:
        repl_tools = list(cli_default_tools())
    if local_mode_enabled():
        from agents.task.constants import AutonomyConfig
        from tools.cronjob_tools import cron_enabled
        if AutonomyConfig.goals_enabled() and container.has_service("goal"):
            repl_tools.append("goal")
        if cron_enabled() and container.has_service("cronjob"):
            repl_tools.append("cronjob")
        # SB-09: the knowledge tool is service-registered under KB_ENABLED (local-ON)
        # but was never added to the session's loaded tool_ids, so agent-driven
        # kb_ingest was unreachable (read-side kb_search rides session_search; ingest
        # did not). Add it when available so "remember this doc" actually works.
        if (AutonomyConfig.kb_enabled() and container.has_service("knowledge")
                and "knowledge" not in repl_tools):
            repl_tools.append("knowledge")

    request = {
        "task": None,            # interactive: no upfront task
        "model": model,
        "provider": provider,
        "tools": repl_tools,   # filesystem/task + enabled autonomy tools (local mode)
        "max_steps": 50,
        "temperature": 0.0,
        "use_vision": False,
    }

    session_id = None
    _autonomy_handles = None  # started post-agent (local profile only); stopped in finally
    _prev_sigint = None       # captured when the REPL SIGINT handler is installed; restored in finally
    try:
        # Create the interactive session (suppress init noise). The restore is
        # guaranteed even if create_session raises (e.g. session-limit), so the
        # error surfaces instead of being swallowed by the redirect.
        # F1: a failed create_session must surface a clean error and return —
        # not escape as a raw traceback out of asyncio.run.  A session-limit
        # error renders the actionable F8 block (shared with `polyrob run`).
        try:
            with _suppress_bootstrap_output():
                session_info = await task_agent.create_session(
                    user_id=user_id,
                    request=request,
                    skip_credit_check=True,
                )
        except Exception as e:
            from cli.commands._errors import echo_create_session_error
            echo_create_session_error(e, user_id)
            return

        session_id = session_info["id"]
        # Scope the feed sink to this session now that we know its id (events from
        # background/foreign sessions are dropped from the foreground UI).
        _repl_sid["id"] = session_id
        # Publish the live session id to the lifecycle ref so the synchronous
        # SIGINT fallback in run_repl can flip the status if the async cancel
        # in `finally` can't run (F2 belt-and-braces).
        if lifecycle_ref is not None:
            lifecycle_ref["session_id"] = session_id

        # Obtain a real Agent. create_session only builds the orchestrator, so we
        # must create the agent ourselves (mirrors run_session's first-task path).
        orchestrator = task_agent.get_orchestrator(session_id)
        if orchestrator is None:
            click.echo(click.style("[polyrob] ERROR: ", fg="red") + "Orchestrator missing after session creation")
            return

        # Wire the init-chosen persona into <identity> via the orchestrator seam
        # that run_session/create_agent already reads (execution.py:124). Fail-open.
        try:
            _persona_text = resolve_cli_persona()
            if _persona_text:
                orchestrator._persona_block = _persona_text
        except Exception:
            pass  # fail-open

        # Phase 3: wire the orchestrator-level stream callback BEFORE the agent
        # is created, mirroring the webview pattern (the orchestrator forwards
        # each LLM output chunk to _on_stream_chunk; create_agent registers it on
        # the agent's hitl_manager).  We never reach into agent.hitl_manager from
        # the CLI.  Filtered to the main agent so a delegate_task sub-agent's
        # stream can't interleave into the box.
        from cli.ui.streaming import make_stream_callback
        orchestrator._on_stream_chunk = make_stream_callback(
            _renderer,
            main_agent_id=lambda: _ui_state.main_agent_id,
        )

        # D3: subscribe to the orchestrator's sub-agent lifecycle hooks so a
        # delegate_task spawn surfaces as a live "N sub-agents" status segment.
        # The hooks already fire (sub_agent_manager.py); nobody subscribed. Fail-open.
        try:
            from cli.ui.live_hooks import make_subagent_hooks
            _sa_start, _sa_end = make_subagent_hooks(_ui_state)
            orchestrator.register_subagent_start_hook(_sa_start)
            orchestrator.register_subagent_end_hook(_sa_end)
        except Exception:
            pass

        # Obtain the executor Agent (guarded: a build failure prints a clean error
        # and returns None instead of escaping as a raw traceback — F1 parity with
        # the create_session guard above).
        agent, agent_preexisted = await _start_repl_agent(
            task_agent, orchestrator, request, session_id
        )
        if agent is None:
            return  # clean error already printed by _start_repl_agent

        # If the agent pre-existed (built during create_session, before we set
        # _on_stream_chunk), create_agent's one-time registration didn't see our
        # callback — register it now via the orchestrator's own method (still the
        # webview seam; we never touch agent.hitl_manager from here).
        if agent_preexisted:
            try:
                await orchestrator._register_stream_callback(agent)
            except Exception:
                pass

        from agents.task.agent.conversation import Conversation
        convo = Conversation(agent)

        # Start the autonomy background loops (cron / goals / curator) under the
        # local profile only, via the SAME shared runtime the server lifespan uses
        # — so `rob` actually runs the loops it gates on. Each loop is independently
        # gated + fail-open; stopped in the finally below. Fail-open: a problem here
        # must never break the interactive session.
        try:
            from agents.task.constants import local_mode_enabled
            if local_mode_enabled():
                from core.autonomy_runtime import start_autonomy
                _data_dir = getattr(container.config, "data_dir", "data")
                _autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
        except Exception:
            pass

        # Resolve the session dir so we can poll llm_usage for live tokens/cost
        # (the live path writes usage to disk, not the push feed — §0 amend. 1).
        session_dir: Optional[Path] = None
        try:
            from agents.task.path import pm
            session_dir = pm().get_session_root(session_id, user_id)
        except Exception:
            session_dir = None
        # Publish to the feed-callback holder so tokens/cost are polled LIVE on every
        # feed event (not just the end-of-turn _poll_usage below).
        _usage_dir["path"] = session_dir
        _agent_ref["agent"] = agent  # so _poll_usage_live can refresh ctx% live

        # Model-identity SSOT: the banner + frame must show the model the BUILT
        # agent actually runs, not the pre-build resolver's request. A pinned but
        # unkeyed provider (e.g. DEFAULT_PROVIDER=nvidia with no key) silently
        # falls back inside _get_llm_for_request, so `model`/`provider` (resolved
        # above) can name a model that never built. Read the truth back off the
        # agent and seed SessionState so the first toolbar paint is correct too.
        _true_model = getattr(agent, "model_name", None) or model
        _true_provider = getattr(agent, "llm_provider", None) or provider
        if not _ui_state.model:
            _ui_state.model = _true_model
        if not _ui_state.provider:
            _ui_state.provider = _true_provider
        # Honest fallback disclosure: if the built model differs from what was
        # requested, say so once instead of silently showing a different model.
        if (_true_model, _true_provider) != (model, provider) and model:
            click.echo(click.style(
                f"[polyrob] requested {model} ({provider}) unavailable — "
                f"running {_true_model} ({_true_provider}).",
                fg="yellow",
            ))

        def _poll_usage() -> None:
            if session_dir is not None:
                _ui_state.poll_usage(session_dir)

        # First-run banner (proposal §9): one compact panel — version,
        # model/provider, tools, short session id, configured-provider key NAMES
        # (never values).  Cosmetic; never block the REPL on it.
        try:
            from core.config import AgentConfig

            from cli.polyrob import VERSION as _ROB_VERSION
            from cli.ui.banner import print_banner
            from core.instance import FRAMEWORK_NAME, resolve_instance_id

            # Session-info line (polyrob framework / instance + user / memory /
            # autonomy). Each resolve is fail-open: a problem degrades the line,
            # never blocks the banner.
            _memory_backend = ""
            try:
                from modules.memory.registry import get_memory_registry
                _prov = get_memory_registry().active()
                if _prov is not None and getattr(_prov, "is_external", False):
                    _memory_backend = str(getattr(_prov, "name", "") or "")
            except Exception:
                _memory_backend = ""
            try:
                from agents.task.constants import local_mode_enabled as _lme
                _autonomy_on = _lme()
            except Exception:
                _autonomy_on = None
            print_banner(
                _renderer,
                version=_ROB_VERSION,
                model=_true_model,
                provider=_true_provider,
                tool_ids=request.get("tools", []),
                session_id=session_id,
                config=AgentConfig(),
                show_help_hint=True,
                framework=FRAMEWORK_NAME,
                instance_id=resolve_instance_id(),
                user_id=user_id,
                memory_backend=_memory_backend,
                autonomy_on=_autonomy_on,
            )
        except Exception:
            pass
        click.echo("")

        # F2/F4: graceful SIGINT lifecycle, adapted from run.py for the REPL.
        # We capture the running loop here (a loop is guaranteed to exist; a bare
        # get_running_loop() inside the handler can raise, since the handler runs
        # on the main thread outside the coroutine scheduler).
        #
        #   first Ctrl-C  → raise KeyboardInterrupt into the loop.  When a turn is
        #     in flight the existing `except KeyboardInterrupt` in
        #     _conversation_loop interrupts the turn; at the prompt boundary it is
        #     caught and the line is dropped (prompt_toolkit owns SIGINT while its
        #     prompt is active, so its Ctrl-C-clears-the-line behaviour is kept —
        #     this handler only fires for the threaded fallback reader / during a
        #     turn).  Either way the KeyboardInterrupt cannot escape asyncio.run:
        #     the `finally` below always runs the async cancel, which now persists
        #     a terminal status (F3).
        #   second Ctrl-C → force exit.  run_repl's synchronous KeyboardInterrupt
        #     fallback is the belt-and-braces that flips the status if asyncio.run
        #     is torn down before the async cancel completes.
        try:
            _repl_loop = asyncio.get_running_loop()
        except RuntimeError:
            _repl_loop = None
        _sigint_count = {"n": 0}

        def _repl_sigint_handler(_sig, _frame):
            _sigint_count["n"] += 1
            if _sigint_count["n"] >= 2:
                click.echo("\n" + click.style("[polyrob] ", fg="red") + "Force exit")
                # Re-raise into the main thread so asyncio.run unwinds through the
                # `finally` (and then run_repl's sync fallback) rather than a hard
                # os._exit that would skip status persistence.
                raise KeyboardInterrupt
            click.echo("\n" + click.style("[polyrob] ", fg="yellow") + "Interrupting (Ctrl+C again to force exit)...")
            raise KeyboardInterrupt

        try:
            _prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _repl_sigint_handler)
        except (ValueError, OSError):
            pass

        # Phase 4: a registry-backed slash dispatcher with full session context
        # (state/session_id/user_id/task_agent/orchestrator) so /status /usage
        # /tools /sessions /resume etc. have their live sources.
        _slash_dispatch = _make_default_slash_dispatch(
            convo,
            container,
            _renderer,
            state=_ui_state,
            session_id=session_id,
            user_id=user_id,
            task_agent=task_agent,
            orchestrator=orchestrator,
        )

        # D5: persistent bottom-anchored input (POLYROB_PERSISTENT_INPUT, default ON).
        # A long-lived Application keeps the input + status pinned at the bottom
        # and live during a turn. Set the flag to 0/off to fall through to the legacy
        # ephemeral prompt_async path.
        from cli.ui.app import persistent_input_enabled as _persistent_enabled
        from cli.ui.theme import is_tty as _is_tty
        if _persistent_enabled() and _is_tty(sys.stdin) and not plain:
            try:
                from cli.ui.commands import build_completer, default_registry

                def _session_ids_p():
                    try:
                        return [
                            str(s.get("id", ""))
                            for s in (task_agent.session_manager.get_all_sessions() or [])
                            if s.get("id")
                        ]
                    except Exception:
                        return []

                _completer_p = build_completer(default_registry(), sessions_provider=_session_ids_p)
                await _run_persistent_app(
                    convo, _ui_state, _renderer, _slash_dispatch, _poll_usage,
                    completer=_completer_p,
                )
                return
            except Exception as e:
                # Fail-open to the legacy loop if the Application can't start.
                click.echo(click.style("[polyrob] ", fg="yellow")
                           + f"persistent input unavailable ({e}); using classic prompt.")

        # Input surface: prompt_toolkit when interactive (live bottom toolbar,
        # FileHistory, Meta/Enter, slash autocomplete); falls back to the
        # threaded input() seam when stdin isn't a TTY (CI/pipes) so the loop
        # still works headlessly.
        read_line = None
        _patch_stdout_cm = contextlib.nullcontext()
        if _is_tty(sys.stdin) and not plain:
            try:
                from prompt_toolkit.patch_stdout import patch_stdout

                from cli.ui.app import build_prompt_session, make_prompt_reader
                from cli.ui.commands import build_completer, default_registry

                # Slash autocomplete: complete /names; offer session ids for
                # /resume from the session manager (best-effort).
                def _session_ids():
                    try:
                        return [
                            str(s.get("id", ""))
                            for s in (task_agent.session_manager.get_all_sessions() or [])
                            if s.get("id")
                        ]
                    except Exception:
                        return []

                _completer = build_completer(
                    default_registry(), sessions_provider=_session_ids
                )
                _session = build_prompt_session(_ui_state, completer=_completer)
                read_line = make_prompt_reader(_session)
                # patch_stdout keeps renderer prints above the pinned prompt.
                # raw=True so the renderer's colored ANSI passes through instead of
                # being escaped to literal "?[…m" text (StdoutProxy.write_raw).
                _patch_stdout_cm = patch_stdout(raw=True)
            except Exception:
                read_line = None
                _patch_stdout_cm = contextlib.nullcontext()

        with _patch_stdout_cm:
            await _conversation_loop(
                convo,
                container,
                read_line=read_line,
                renderer=_renderer,
                on_turn_complete=_poll_usage,
                slash_dispatch=_slash_dispatch,
            )
    finally:
        ProductTelemetry._on_feed_entry = None
        # Restore the SIGINT handler we installed (avoid leaking the REPL's handler
        # if run_repl is ever embedded/called from another entrypoint).
        if _prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, _prev_sigint)
            except (ValueError, OSError, TypeError):
                pass
        if _autonomy_handles is not None:
            try:
                await _autonomy_handles.stop()
            except Exception:
                pass
        if session_id:
            try:
                await task_agent.cancel_session(user_id=user_id, session_id=session_id)
                # The async cancel completed → the session is already at a
                # terminal status (F3).  Mark it so run_repl's synchronous
                # fallback doesn't redundantly flip it again.
                lifecycle_ref["cleaned"] = True
            except Exception:
                pass


def _repl_sync_cleanup(lifecycle_ref: dict) -> None:
    """Synchronous best-effort terminal-status flip for a SIGINT-torn exit (F2).

    The async ``cancel_session`` in ``_repl_main``'s ``finally`` is the primary
    path; it persists ``cancelled`` (F3).  But if a Ctrl-C tears ``asyncio.run``
    down before that await can complete, the session would otherwise leak on
    disk as ``created``.  This belt-and-braces flips it via the **session_manager
    API only** (never the raw metadata path), using the objects ``_repl_main``
    already published to ``lifecycle_ref``.  No-op when the async cancel already
    ran (``cleaned``) or when there's nothing to clean.
    """
    if lifecycle_ref.get("cleaned"):
        return
    session_id = lifecycle_ref.get("session_id")
    task_agent = lifecycle_ref.get("task_agent")
    if not session_id or task_agent is None:
        return
    try:
        sm = getattr(task_agent, "session_manager", None)
        if sm is not None:
            sm.update_session_status(session_id, "cancelled")
            lifecycle_ref["cleaned"] = True
    except Exception:
        pass


def run_repl(plain: bool = False, *, model: Optional[str] = None,
             provider: Optional[str] = None, toolset: Optional[str] = None):
    """Synchronous entry point for the REPL.

    plain: force the plain renderer (the top-level ``--plain`` flag).
    model/provider/toolset: optional launch overrides (parity with ``polyrob run``).

    F2: wraps ``asyncio.run`` so a SIGINT that escapes the event loop (e.g. a
    forced second Ctrl-C, or a Ctrl-C landing during teardown) still flips the
    interactive session to a terminal status via the synchronous fallback —
    no leaked ``created`` sessions.
    """
    lifecycle_ref: dict = {}
    try:
        asyncio.run(_repl_main(plain=plain, lifecycle_ref=lifecycle_ref,
                               model=model, provider=provider, toolset=toolset))
    except KeyboardInterrupt:
        _repl_sync_cleanup(lifecycle_ref)
    else:
        # Even on a clean exit, ensure the status is terminal if the async
        # cancel somehow didn't run (idempotent).
        _repl_sync_cleanup(lifecycle_ref)
