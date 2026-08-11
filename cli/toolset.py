"""Shared CLI tool-list resolution — ONE resolver for `polyrob run` and the REPL.

`cli/commands/run.py` and `cli/commands/chat.py` each carried their own copy of
this precedence chain and the copies drifted (the REPL's explicit ``--toolset``
branch skipped the ``cli_unavailable_tools`` prune). This module is the single
home; ``run.py`` re-imports it under its legacy `_resolve_tool_list` name and the
REPL layers its local-mode additions (goal/cronjob/knowledge) on top of the
returned list.
"""
import os
from typing import Optional

from core.runtime_paths import data_dir_or_home


def resolve_tool_list(
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
