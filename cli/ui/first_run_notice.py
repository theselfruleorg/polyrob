"""first_run_notice.py — the ONE-TIME posture notice for the POLYROB CLI (0.9.0).

Printed once per data home (keyed off a ``.polyrob_welcomed`` marker) at REPL /
``polyrob run`` start, so a new user is told up front what is running and where:

    Welcome to polyrob.
      Autonomy: OFF — the agent acts only on your messages; it will not schedule
        goals, self-wake, or edit its own skills.
      Data dir: /Users/…/.polyrob
      Config: /Users/…/.polyrob/.env
      Interactive tools on: coding, git, knowledge base, memory/RAG, project-context.
      Enable autonomy: set AUTONOMY_ENABLED=true (or run `polyrob init`). See
        `polyrob doctor`.

Autonomy is OFF by default for new local installs; this notice is how the user
learns that (and where their data/config live) without reading the docs.

Design (mirrors ``banner.py``): a PURE builder (``build_first_run_notice``) plus a
single fail-open I/O entry (``maybe_print_first_run_notice``). The notice is
context, not content — any error (unwritable marker, resolver failure, renderer
problem) must degrade to silence, never block the CLI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

_MARKER = ".polyrob_welcomed"


def build_first_run_notice(
    *, autonomy_on: bool, data_dir: str, config_path: Optional[str]
) -> List[str]:
    """Pure builder — the notice lines (no I/O)."""
    lines = ["Welcome to polyrob."]
    if autonomy_on:
        lines.append(
            "  Autonomy: ON — the agent may work on its own between your messages "
            "(schedule goals, self-wake, edit its own skills)."
        )
    else:
        lines.append(
            "  Autonomy: OFF — the agent acts only on your messages; it will not "
            "schedule goals, self-wake, or edit its own skills."
        )
    lines.append(f"  Data dir: {data_dir}")
    if config_path:
        lines.append(f"  Config: {config_path}")
    lines.append(
        "  Interactive tools on: coding, git, knowledge base, memory/RAG, project-context."
    )
    if not autonomy_on:
        lines.append(
            "  Enable autonomy: set AUTONOMY_ENABLED=true (or run `polyrob init`). "
            "See `polyrob doctor`."
        )
    return lines


def marker_path(data_dir: str) -> Path:
    return Path(data_dir) / _MARKER


def _resolve_context() -> tuple:
    """(autonomy_on, data_dir, config_path) from the live process env. Fail-soft:
    any piece that can't resolve degrades to a safe value, never raises."""
    from agents.task.constants import autonomy_enabled, local_mode_enabled

    autonomy_on = bool(autonomy_enabled())
    try:
        from core.runtime_paths import resolve_data_home
        data_dir = str(resolve_data_home())
    except Exception:
        data_dir = "data"
    config_path = None
    try:
        from core.paths import env_file_candidates
        for c in env_file_candidates(local_mode=local_mode_enabled()):
            if c.path.exists():
                config_path = str(c.path)
                break
    except Exception:
        config_path = None
    return autonomy_on, data_dir, config_path


def maybe_print_first_run_notice(renderer: Any) -> bool:
    """Print the posture notice ONCE per data home; return True if printed.

    Keyed on a ``.polyrob_welcomed`` marker under the resolved data dir. Fail-open:
    any error returns False silently (the notice is orientation, not content)."""
    try:
        autonomy_on, data_dir, config_path = _resolve_context()
        mp = marker_path(data_dir)
        try:
            if mp.exists():
                return False
        except Exception:
            return False  # can't check the marker → stay quiet rather than spam
        lines = build_first_run_notice(
            autonomy_on=autonomy_on, data_dir=data_dir, config_path=config_path
        )
        text = "\n".join(lines)
        console = getattr(renderer, "console", None)
        if console is not None:
            console.print(text)
        else:
            renderer.print_block(text)
        # Best-effort marker write — a failure just means the notice re-shows next
        # run, which is preferable to crashing the CLI on an unwritable data dir.
        try:
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text("polyrob first-run notice shown\n")
        except Exception:
            pass
        return True
    except Exception:
        return False
