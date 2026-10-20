"""Build the <environment> foundation block (proposal 014-C1).

Tells the agent WHERE IT LIVES — instance, platform, data dir, workspace
persistence semantics, capability-axis levels, and which
developer executables exist on the host — so "I don't know where I am /
whether files persist / whether node exists" is answerable in-context
(2026-07-15 incident: a render task stalled on exactly those unknowns).

Emission policy (014 D-3): ONLY under ``local_mode_enabled()`` OR
``full_autonomy_enabled()`` — a multi-tenant server tenant never sees host
details. Gated ``ENV_CONTEXT_BLOCK`` (default ON; fail-open — any builder
error returns None and session construction continues without the block).

Content is per-session-STABLE: built once at session construction and pinned
as a foundation message (``MessageManager.set_environment_message``), NOT in
the system prompt, so prompt caching is untouched (same PR13 rationale as
skills/self-context/runtime-identity). The absolute workspace path is
per-SESSION, so it is its own message after the catalogs
(``build_workspace_note``, 081 WS-3.3).

Secret hygiene: emits only paths, level names, and a ``shutil.which`` probe of
executable NAMES — never an env VALUE; the rendered block still passes through
``scrub_secret_shapes`` as a defensive backstop (same pattern as the
``agent_status`` action).
"""
from __future__ import annotations

import os
import platform
import shutil
from functools import lru_cache
from typing import Optional

_PROBE_BINARIES = ("node", "npm", "npx", "docker", "git", "python3")


@lru_cache(maxsize=1)
def _host_capabilities() -> str:
    present = [b for b in _PROBE_BINARIES if shutil.which(b)]
    return ", ".join(present) if present else "(none detected)"


def _enabled() -> bool:
    from core.env import bool_env
    if not bool_env("ENV_CONTEXT_BLOCK", True):
        return False
    from core.config_policy import local_mode_enabled, full_autonomy_enabled
    return bool(local_mode_enabled() or full_autonomy_enabled())


def build_workspace_note(session_id: str, user_id: Optional[str]) -> Optional[str]:
    """081 WS-3.3: the session's workspace path — the one per-session line of
    the environment, pinned AFTER the catalogs (``FOUNDATION_LAYERS``) so the
    ``<environment>`` block is the same for every session on the install.
    None when the environment block is off. Never raises."""
    try:
        if not _enabled():
            return None
        from agents.task.path import pm
        workspace = str(pm().get_workspace_dir(pm().clean_session_id(session_id), user_id))
        return f"<session-workspace>\nWorkspace (absolute): {workspace}\n</session-workspace>"
    except Exception:
        return None


def build_environment_context(session_id: str, user_id: Optional[str],
                              tool_ids=None) -> Optional[str]:
    """Return the rendered <environment> block, or None (disabled / server / error)."""
    try:
        if not _enabled():
            return None
        from core.config_policy import (autonomy_enabled, autonomy_mode_display,
                                           autonomy_posture, compute_posture)
        from core.instance import resolve_instance_id

        from core.runtime_paths import data_dir_or_home
        data_dir = data_dir_or_home(os.getenv("POLYROB_DATA_DIR"))
        shared = bool(os.getenv("POLYROB_PROJECT_DIR"))
        persistence = (
            "Files you write in the workspace PERSIST on this machine across "
            "sessions (shared project workspace)." if shared else
            "Files you write in the workspace persist on disk but are scoped "
            "to THIS session's directory."
        )
        lines = [
            "<environment>",
            f"Instance: {resolve_instance_id()} | Platform: "
            f"{platform.system()} {platform.release()}",
            f"Data dir: {data_dir}",
            persistence,
            # 018 P4: autonomy_mode_display() is the CLAMPED truth — raw
            # autonomy_mode() could tell the agent 'autonomous' while the
            # single-owner guard has clamped it to supervised. Both axes shown.
            f"Capability axes: compute posture {compute_posture()} "
            f"(0=confined, 1=sandbox-dev, 2=self-maintain, 3=host), "
            f"autonomy mode '{autonomy_mode_display()}', "
            f"autonomy posture '{autonomy_posture()}', "
            f"self-directed autonomy loops "
            f"{'ENABLED' if autonomy_enabled() else 'OFF'} (AUTONOMY_ENABLED) — "
            f"when OFF you act only on the user's messages (no self-wake, goal "
            f"scheduling, or skill self-editing).",
            f"Host executables visible to enabled exec tools: {_host_capabilities()}.",
            "For live status (steps/tools/context/budget/health) call agent_status. "
            "A <live-health> note at the start of each turn carries the same health "
            "facts the owner's /status shows.",
            "</environment>",
        ]
        # 062: how this install was made, and what it cannot do. Without these
        # the agent can report a refusal but never explain it.
        try:
            from core.install_facts import install_lines
            for _line in install_lines():
                lines.insert(-2, _line)
        except Exception:
            pass

        from core.config_policy.policy import autonomous_run_budget_usd, run_budget_usd
        _budget = run_budget_usd()
        if _budget > 0:
            lines.insert(-2, f"Run budget: RUN_BUDGET_USD=${_budget:.2f} session provider-spend "
                             "ceiling — the run halts at the cap (value at session start).")
        # 083 WS-3: install-wide (the block is the same for every session).
        _run_cap = autonomous_run_budget_usd()
        if _run_cap > 0:
            lines.insert(-2, f"Autonomous run budget: AUTONOMOUS_RUN_BUDGET_USD=${_run_cap:.2f} "
                             "provider spend per cron/goal run, sub-agents included — the "
                             "run halts at the cap.")
        if tool_ids:
            # Positive tool list (018 P4): <tool-availability> enumerates only
            # ABSENT tools; the loaded set previously cost an agent_status call.
            lines.insert(-2, "Tools loaded this session: "
                         + ", ".join(sorted(str(t) for t in tool_ids)) + ".")
        text = "\n".join(lines)
        try:
            from core.secret_scrub import scrub_secret_shapes
            text = scrub_secret_shapes(text)
        except Exception:
            pass  # scrubber unavailable — content is path/level-only by construction
        return text
    except Exception:
        return None  # fail-open: never block session construction
