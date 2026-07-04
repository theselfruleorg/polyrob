"""POLYROB CLI UI package — event model, state, and renderers.

``select_renderer`` is the single place the surface (REPL / one-shot) decides
between the Rich and Plain renderers based on TTY / ``NO_COLOR`` / ``--plain``.
"""

from __future__ import annotations

from typing import Any

from cli.ui.renderer import Renderer
from cli.ui.state import SessionState
from cli.ui.theme import is_tty, no_color


def use_rich(*, plain: bool = False, stream: Any = None) -> bool:
    """Decide whether the Rich renderer should be used.

    Rich is used only when: stdout is a TTY, ``NO_COLOR`` is unset, and the
    caller didn't force ``--plain``.  Otherwise the deterministic plain
    renderer is selected (CI, pipes, ``NO_COLOR``, ``TERM=dumb``).
    """
    if plain:
        return False
    if no_color():
        return False
    return is_tty(stream)


def select_renderer(
    state: SessionState,
    *,
    plain: bool = False,
    stream: Any = None,
    one_shot: bool = False,
    console: Any = None,
    live_allowed: bool = True,
) -> Renderer:
    """Construct the appropriate concrete ``Renderer`` for the current terminal.

    Args:
        state:    Shared ``SessionState``.
        plain:    Force the plain renderer (``--plain``).
        stream:   Output stream for TTY detection / plain output.
        one_shot: ``polyrob run`` one-shot context (completion panel shows result).
        console:  Optional Rich ``Console`` override (tests).
        live_allowed: When False (the REPL, which runs under prompt_toolkit's
            ``patch_stdout``) the Rich renderer suppresses its in-place ``Live``
            regions to avoid cursor corruption — see ``RichRenderer``. ``rob run``
            leaves this True. No effect on the plain renderer.
    """
    if use_rich(plain=plain, stream=stream):
        from cli.ui.rich_renderer import RichRenderer

        return RichRenderer(
            state=state, console=console, one_shot=one_shot, live_allowed=live_allowed
        )

    from cli.ui.plain_renderer import PlainRenderer

    return PlainRenderer(state=state, stream=stream, one_shot=one_shot)


__all__ = ["Renderer", "SessionState", "select_renderer", "use_rich"]
