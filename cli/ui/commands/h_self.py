"""h_self.py — the ``/self`` REPL slash-command handler.

Read-only view of the running POLYROB instance's IDENTITY:

- framework name + instance id + bound owner,
- the operator-authored, frozen **SOUL** self-context (``<home>/identity/*.md``),
- the agent-writable **SELF** doc (``<home>/identity/{instance}/user_{uid}/self.md``).

This is a pure *reader*. It never writes identity docs — the write path is the
separate ``self_context_manage`` agent action. It mirrors the exact
``home_dir``/``load_self_context``/``load_self_doc`` call pattern that
``agents/task/agent/core/construction.py`` uses to pin the SELF_CONTEXT
foundation message, so what ``/self`` shows matches what the running agent sees.

Kept in its own module (the god-file split convention — new behavior gets its
own file, see ``handlers.py``'s header). Registration is wired by the REPL /
default-registry builder, not here. Fail-open: any backend error degrades to a
friendly one-liner (never raises into the REPL).
"""

from __future__ import annotations

from typing import Any

from cli.ui.commands.registry import CommandContext
from core.runtime_paths import data_dir_or_home

# 113 W6 G-6: the one `/self` text every seat renders.
from surfaces.telegram.self_memory_ops import (  # noqa: E402,F401 — the shared reply
    DOC_PREVIEW_CHARS as _DOC_PREVIEW_CHARS,
    TRUNCATION_MARKER as _TRUNCATION_MARKER,
    preview as _preview,
)


def _resolve_home_dir(ctx: CommandContext) -> Any:
    """Resolve the POLYROB home dir the SAME way construction.py does.

    construction.py pins the SELF_CONTEXT foundation message from the container
    config's ``data_dir`` (fallback ``"data"``); mirror it so ``/self`` reads the
    identical ``<home>/identity/`` tree the running agent was seeded from.
    """
    try:
        cfg = getattr(ctx.container, "config", None)
        return data_dir_or_home(getattr(cfg, "data_dir", None))
    except Exception:
        return data_dir_or_home(None)


def h_self(ctx: CommandContext) -> None:
    """Show the running instance's identity: SOUL + SELF docs (read-only).

    The chat seat's own reply (``surfaces.telegram.self_memory_ops.self_reply``,
    113 W6 G-6): the phone and the terminal read one text. Fail-open: an error
    degrades to one line, never raises into the REPL.
    """
    try:
        from surfaces.telegram.self_memory_ops import self_reply
        ctx.emit(self_reply(ctx.user_id or "local", _resolve_home_dir(ctx)), title="self")
    except Exception as exc:
        ctx.emit(f"Could not resolve self-context: {exc}", title="self")
