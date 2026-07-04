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
    devnull = open(os.devnull, "w")
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
