"""Build the <environment> foundation block (proposal 014-C1).

Tells the agent WHERE IT LIVES — instance, platform, data dir, absolute
workspace path + persistence semantics, capability-axis levels, and which
developer executables exist on the host — so "I don't know where I am /
whether files persist / whether node exists" is answerable in-context
(Remotion incident, docs/reviews/2026-07-15-remotion-incident-tooling-env-awareness.md).

Emission policy (014 D-3): ONLY under ``local_mode_enabled()`` OR
``full_autonomy_enabled()`` — a multi-tenant server tenant never sees host
details. Gated ``ENV_CONTEXT_BLOCK`` (default ON; fail-open — any builder
error returns None and session construction continues without the block).

Content is per-session-STABLE: built once at session construction and pinned
as a foundation message (``MessageManager.set_environment_message``), NOT in
the system prompt, so prompt caching is untouched (same PR13 rationale as
skills/self-context/runtime-identity).

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
    from agents.task.constants import local_mode_enabled, full_autonomy_enabled
    return bool(local_mode_enabled() or full_autonomy_enabled())


def build_environment_context(session_id: str, user_id: Optional[str],
                              tool_ids=None) -> Optional[str]:
    """Return the rendered <environment> block, or None (disabled / server / error)."""
    try:
        if not _enabled():
            return None
        from agents.task.constants import (autonomy_mode_display, autonomy_posture,
                                           compute_posture)
        from agents.task.path import pm
        from core.instance import resolve_instance_id

        workspace = str(pm().get_workspace_dir(pm().clean_session_id(session_id), user_id))
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
            f"Workspace (absolute): {workspace}",
            persistence,
            # 018 P4: autonomy_mode_display() is the CLAMPED truth — raw
            # autonomy_mode() could tell the agent 'autonomous' while the
            # single-owner guard has clamped it to supervised. Both axes shown.
            f"Capability axes: compute posture {compute_posture()} "
            f"(0=confined, 1=sandbox-dev, 2=self-maintain, 3=host), "
            f"autonomy mode '{autonomy_mode_display()}', "
            f"autonomy posture '{autonomy_posture()}'.",
            f"Host executables visible to enabled exec tools: {_host_capabilities()}.",
            "For live status (steps/tools/context/budget) call agent_status.",
            "</environment>",
        ]
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
