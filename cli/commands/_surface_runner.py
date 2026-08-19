"""Shared envelope for the single-surface network commands.

The seven network-surface commands (slack, discord, signal, x, whatsapp,
telegram, email) run the SAME lifecycle around a surface-specific harness:
env defaults -> bootstrap -> key preflight -> credential resolution ->
log-level ladder -> container build -> TaskAgent guard -> surface bus +
outbound dispatcher -> harness construction -> autonomy runtime -> online
announce -> signal handlers -> run -> identical finally-teardown. This module
is the ONE home for that envelope; each command file keeps only its option
parsing, credential resolution, and harness construction.

Drift resolved UP here (2026-08-09):
- signal handling uses ``add_signal_handler`` with the ``signal.signal``
  fallback (discord's robust pattern) for EVERY surface — slack/signal
  previously swallowed the failure with no fallback;
- the container data_dir is computed ONCE and passed everywhere
  (whatsapp/telegram/email used to compute it twice);
- quiet mode always squelches gRPC/glog env noise (``GRPC_VERBOSITY`` /
  ``GLOG_minloglevel``) — previously only whatsapp/telegram/email did;
- per-surface noisy-logger silencing rides the ``silence_loggers`` parameter
  (discord's pattern, kept per-surface rather than globalized).

All heavy imports stay INSIDE ``run_surface`` at call time so tests can
monkeypatch the source-module attributes (``core.bootstrap.build_cli_container``,
``surfaces.*.harness.build_*``, ``agents.task.constants.local_mode_enabled``, …)
and so the command modules import cleanly with no surface env set.

``gateway.py`` deliberately does NOT use this runner: it launches MULTIPLE
surfaces in one process with per-surface WARN+skip semantics — a different
shape (and its tests pin its source).
"""
from __future__ import annotations

import asyncio
import os
import signal as _signal
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import click


@dataclass
class SurfaceContext:
    """Everything the runner resolved before handing off to ``build_harness``."""

    container: Any
    task_agent: Any
    data_dir: str          # computed ONCE via data_dir_or_home(container.config.data_dir)
    creds: Any             # whatever resolve_credentials returned (None if no resolver)
    log_level: str         # the resolved ladder level (DEBUG / INFO / ERROR)


@dataclass
class SurfaceJob:
    """What a surface's ``build_harness`` hands back to the runner."""

    harness: Any                                              # must expose async stop()
    run: Callable[[], Awaitable[None]]                        # main-loop coroutine factory
    announce: Optional[Callable[[], Awaitable[None]]] = None  # "online" echoes
    on_stop: Optional[Callable[[], None]] = None              # extra sync action on SIGINT/SIGTERM
    cancel_on_stop: bool = True    # False => on_stop alone triggers a graceful exit (uvicorn)
    guard_harness_stop: bool = False  # wrap the final harness.stop() in try/except


async def run_surface(
    *,
    extra_env: Mapping[str, str],
    verbose: bool,
    build_harness: Callable[[SurfaceContext], Awaitable[SurfaceJob]],
    stopping_message: str,
    resolve_credentials: Optional[Callable[[], Any]] = None,
    preflight: Optional[Callable[[Any, Any], None]] = None,
    post_bus: Optional[Callable[[Any], None]] = None,
    autonomy_precheck: Optional[Callable[[], bool]] = None,
    silence_loggers: Sequence[str] = (),
) -> None:
    """Run one network surface end to end.

    Hook order (each optional hook is a seam for one surface's divergence):
      1. ``extra_env`` setdefault'd (plus ``SINGULAR_CHAT_ENABLED``) BEFORE the
         container build so the bus installs during TaskAgent construction too;
      2. ``resolve_credentials()`` after ``preflight_or_onboard`` (env layers
         loaded) and before the container build — fail fast on a missing token
         (a clean ClickException) instead of paying a full container build;
      3. ``preflight(container, task_agent)`` after the TaskAgent guard and
         BEFORE ``install_surface_bus`` — a misconfigured surface exits without
         side effects (whatsapp/email credential checks);
      4. ``post_bus(container)`` right after ``install_surface_bus`` (whatsapp's
         voice-readiness signal);
      5. ``build_harness(ctx)`` — construct (and optionally start) the harness;
         exceptions here propagate (no teardown has anything to unwind yet);
      6. ``autonomy_precheck()`` inside the fail-open autonomy block — return
         False to skip ``start_autonomy`` after doing your own logging (email's
         EMAIL_AUTONOMY_RUNTIME gate).
    """
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)
    from core.runtime_paths import data_dir_or_home

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (a daemon must not
    # block on an interactive wizard). Loads env layers internally.
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    # Surface + bus gates default ON for this command (explicit env OR FILE
    # value wins — setdefault never clobbers), AFTER the preflight's load_env
    # so a `polyrob config set TELEGRAM_SURFACE_ENABLED false`-style file value
    # is in os.environ before the setdefault (026 P1.3 — the old order seeded
    # these BEFORE load_env, whose override=False then silently ignored the
    # file), and BEFORE the container build so the bus installs during
    # TaskAgent construction too.
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    for _key, _value in (extra_env or {}).items():
        os.environ.setdefault(_key, _value)

    # Resolve surface credentials NOW — preflight_or_onboard already loaded the
    # env layers (./.polyrob/.env etc.), so fail fast on a missing token.
    creds = resolve_credentials() if resolve_credentials is not None else None

    # A headless run (systemd: stderr is the journal, not a TTY) MUST log — a
    # server with no logs is unoperable. An interactive run keeps the console
    # clean. `-v` always gives DEBUG. So: verbose->DEBUG; headless->INFO to the
    # journal; interactive->quiet.
    headless = not sys.stderr.isatty()
    if verbose:
        log_level = "DEBUG"
    elif headless:
        log_level = "INFO"
    else:
        log_level = "ERROR"
    quiet = (not verbose) and (not headless)
    if quiet:
        _logging.disable(_logging.CRITICAL)
        os.environ["GRPC_VERBOSITY"] = "ERROR"
        os.environ["GLOG_minloglevel"] = "3"

    try:
        container = await build_cli_container(log_level=log_level)
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + f"failed to start: {e}")
        sys.exit(1)
    if quiet:
        _logging.disable(_logging.NOTSET)
    elif not verbose:
        # Headless service: app logs at INFO in the journal, but silence the
        # surface's noisy transport libraries.
        for _noisy in silence_loggers:
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "TaskAgent not available in container")
        sys.exit(1)

    # Surface-specific post-container preflight (credential checks against
    # env/config) — before the bus installs, so a misconfigured surface exits
    # without side effects.
    if preflight is not None:
        preflight(container, task_agent)

    # Install the outbound surface bus (idempotent; gated on
    # SINGULAR_CHAT_ENABLED) so agent replies can route out to the surface.
    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)  # db_path defaults to container.config.data_dir

    if post_bus is not None:
        post_bus(container)

    # Start the outbound delivery dispatcher (may be None when the bus is
    # disabled). Without this, when durable outbound is enabled replies enqueue
    # and NEVER send. Sync start(), async stop().
    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        from cli.commands._bootstrap import attach_dispatcher_event_log
        attach_dispatcher_event_log(dispatcher)
        dispatcher.start()

    # Pin harness state DBs (dedup / directories) to the container's data_dir so
    # per-instance isolation holds under POLYROB_DATA_DIR (else they land in
    # ./data). Computed ONCE for harness + autonomy.
    data_dir = data_dir_or_home(
        getattr(getattr(container, "config", None), "data_dir", None))

    # 027 WP3 backstop: a surface whose extra slipped past its command-level
    # preflight must still fail with the pip remedy, not a raw traceback.
    try:
        job = await build_harness(SurfaceContext(
            container=container, task_agent=task_agent, data_dir=data_dir,
            creds=creds, log_level=log_level))
    except ModuleNotFoundError as exc:
        from core.optional_extras import missing_extra_hint

        hint = missing_extra_hint(str(exc))
        if hint:
            click.echo(
                click.style("[polyrob] ERROR: ", fg="red") + hint, err=True)
            sys.exit(1)
        raise

    # Start the autonomy background loops (cron/goals/curator/surface GC) under
    # the local profile — the SAME shared runtime the REPL + API lifespan use.
    # Fail-open.
    autonomy_handles = None
    try:
        if autonomy_precheck is None or autonomy_precheck():
            from agents.task.constants import local_mode_enabled
            if local_mode_enabled():
                from core.autonomy_runtime import start_autonomy
                autonomy_handles = start_autonomy(task_agent=task_agent,
                                                  data_dir=data_dir)
    except Exception:
        autonomy_handles = None

    if job.announce is not None:
        await job.announce()

    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(job.run())

    def _stop(*_a):
        if job.on_stop is not None:
            job.on_stop()
        if job.cancel_on_stop:
            run_task.cancel()

    # Robust install (discord's pattern, resolved UP for all surfaces):
    # add_signal_handler where the loop supports it, else plain signal.signal.
    try:
        loop.add_signal_handler(_signal.SIGINT, _stop)
        loop.add_signal_handler(_signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        for _sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                _signal.signal(_sig, lambda *_a: _stop())
            except (ValueError, OSError, AttributeError):
                pass  # SIGTERM may be unavailable on some platforms/threads

    try:
        await run_task
    except asyncio.CancelledError:
        pass
    finally:
        click.echo("\n" + click.style(stopping_message, dim=True))
        if autonomy_handles is not None:
            try:
                await autonomy_handles.stop()
            except Exception:
                pass
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                pass
        if job.guard_harness_stop:
            try:
                await job.harness.stop()
            except Exception:
                pass
        else:
            await job.harness.stop()
