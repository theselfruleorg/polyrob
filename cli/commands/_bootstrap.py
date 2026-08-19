"""Shared CLI bootstrap helpers (Phase 5).

The single home for the *narrow* bootstrap-output suppression used by both the
REPL (``chat.py``) and the one-shot path (``run.py``).  Proposal §9: the
whole-phase ``/dev/null`` redirect is gone — we suppress only the noisy MCP /
gRPC bootstrap prints that arrive on stderr during ``build_cli_container`` and
session creation.  Once the renderer is live it owns stdout, and any error
raised inside the window surfaces (the restore is in ``finally``).
"""

from __future__ import annotations

import contextlib
import os
import sys


def attach_dispatcher_event_log(dispatcher) -> None:
    """Best-effort wiring: give a started ``OutboundDispatcher`` the durable telemetry
    event log so ``dead_target_skipped``/``dead_target_marked`` are actually recorded
    outside tests.

    Bootstrap (``core/surfaces/bootstrap.py``) deliberately constructs the dispatcher
    with ``event_log=None`` — the core tier must never import ``agents.*``. The CLI
    tier sits above ``agents`` in the layering (tier 4 -> tier 2, a downward import),
    so it is the seam that closes the gap. Called from every ``dispatcher.start()``
    site under ``cli/commands/``. Fail-open: telemetry must never block a surface
    from starting, so any import/lookup error is swallowed silently.
    """
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
    except Exception:
        return
    try:
        if event_log_enabled():
            dispatcher.attach_event_log(get_event_log())
    except Exception:
        pass


#: Once-guard for :func:`ensure_env_loaded` (per process).
_env_loaded = False


def ensure_env_loaded() -> None:
    """Load the local env-file ladder exactly once per process (026 P1.2).

    Failure mode C in proposal 026: 14 command modules (`polyrob cron`,
    `polyrob goals`, `polyrob tools`, …) never called ``load_env``, so a value
    written by ``polyrob config set`` was invisible to them — ``cron schedule``
    warned "CRON_ENABLED is off" right after the owner turned it on. Every
    command GROUP callback that reads flags/credentials calls this seam instead
    of growing its own copy of the setup_project_path/setup_sqlite_compat/
    load_env preamble. Memoized: a group whose subcommand also builds a
    container (which loads env again with override=False) stays idempotent.
    """
    global _env_loaded
    if _env_loaded:
        return
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    _env_loaded = True


async def cli_container(log_level: str = "ERROR"):
    """Standard non-interactive container bootstrap for admin subcommands.

    The one home for the preamble every ``polyrob session …`` verb repeated
    verbatim: project-path + sqlite-compat setup, log squelch around the noisy
    build, the non-interactive key preflight (exits 1 when keys are missing —
    same as the inline copies did), and ``build_cli_container``.  Returns the
    built container with logging restored.
    """
    import logging as _logging

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level=log_level)
    _logging.disable(_logging.NOTSET)
    return container


@contextlib.contextmanager
def suppress_bootstrap_output():
    """Silence Python-level stdout/stderr plus OS fd 2 for a bootstrap window.

    ``sys.stdout``/``sys.stderr`` are rebound to ``/dev/null`` (the MCP config
    loader prints on stdout) and fd 2 is dup'd over so C-extension/gRPC noise
    is caught too.  OS fd 1 is intentionally left alone: a live renderer's
    ``Console`` holds the real file object captured before this window, so it
    is never clobbered.  Note: ``sys.stdout`` (the Python object) IS rebound
    below; "left alone" refers only to OS-level fd 1, which C extensions write
    to directly.  Restoration lives in ``finally`` so an exception raised
    inside the block — e.g. the per-user session-limit ``AgentError`` from
    ``create_session`` — cannot leave output pointed at ``/dev/null`` (which
    would make the error invisible to the user).
    """
    # 027 rider: the window used to discard fd-2 entirely — a native-lib
    # failure during startup (exactly where the no-playwright break landed)
    # vanished. Tee suppressed output into <data_home>/logs/bootstrap.log
    # instead; fail-open to /dev/null (this path only runs past the key gate,
    # where the data home gets created anyway).
    sink = None
    try:
        from core.runtime_paths import resolve_data_home
        log_dir = resolve_data_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = open(log_dir / "bootstrap.log", "a")
    except Exception:
        sink = None
    devnull = sink or open(os.devnull, "w")
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    try:
        saved_fd2 = os.dup(2)
    except Exception:
        devnull.close()
        raise
    sys.stdout = devnull
    sys.stderr = devnull
    os.dup2(devnull.fileno(), 2)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        os.dup2(saved_fd2, 2)
        os.close(saved_fd2)
        devnull.close()
