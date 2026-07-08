"""Persistent `shell` tool (computer-use parity WS-2) + `process` job manager (WS-3).

A stateful shell surface for a posture-entitled OWNER session: env + cwd persist
across `shell_run` calls (Hermes' snapshot-replay model — capture cwd/env after each
command, replay next call; NOT a long-lived interactive shell, which deadlocks).
At AGENT_COMPUTE_POSTURE>=1 every command runs INSIDE the session's persistent docker
sandbox container (`docker exec`); posture 3 (host) is a deliberate single-tenant-box
tier (deferred here). Gated by `compute_posture_allows(ctx, 1)` — never reachable by a
correspondent/leaf/forged turn — and registered into DELEGATE_BLOCKED_TOOLS, out of
CHILD_INHERITABLE_TOOLS and the default tool_ids.

Registration mirrors `register_code_exec_tool`: descriptor + class registered only
when the compute posture makes the tool reachable.
"""
from __future__ import annotations

import logging

from core.env import bool_env as _bool_env


def shell_tools_enabled() -> bool:
    """Whether the `shell`/`process` tools are registered.

    Reachable only at AGENT_COMPUTE_POSTURE>=1 (the tool is inert below that, and the
    per-call gate still requires an owner/non-leaf/non-forged session). An explicit
    ``SHELL_TOOLS_ENABLED`` env always wins (e.g. force-off even at a raised posture).
    """
    try:
        from agents.task.constants import compute_posture
        default = compute_posture() >= 1
    except Exception:
        default = False
    return _bool_env("SHELL_TOOLS_ENABLED", default)


def register_shell_tools(force: bool = False) -> bool:
    """Register the `shell` + `process` descriptors + classes IFF reachable (or forced).

    Returns True when the shell tool was registered. No-op (returns False) at posture 0
    so a default server is byte-identical. Neither tool is ever in the default
    ``tool_ids`` — a session opts in (WS-8 provisions them for posture>=1 goal/cron runs).
    """
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.shell.tool import ShellTool
    from tools.shell.process_tool import ProcessTool

    reg_shell = register_optional_tool(
        "shell",
        ShellTool,
        ToolDescriptor(
            name="shell",
            description="Run shell commands in a persistent sandbox (cwd/env persist; background jobs)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        shell_tools_enabled,
        force=force,
    )
    try:
        register_optional_tool(
            "process",
            ProcessTool,
            ToolDescriptor(
                name="process",
                description="Manage background shell jobs (list/poll/log/kill)",
                category=ToolCategory.INTEGRATION,
                is_optional=True,
                init_priority=80,
            ),
            shell_tools_enabled,
            force=force,
        )
    except Exception:  # never block the shell tool on the process seam
        logging.getLogger(__name__).debug("process tool registration skipped", exc_info=True)
    return reg_shell


__all__ = ["shell_tools_enabled", "register_shell_tools"]
