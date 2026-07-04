"""Render the avatar mesh LIVE in the terminal — truecolor half-blocks, animated.

The face is computed from the engine field port (modules/pfp/mesh) — no PNG, no
Chromium, no Node. Each character is two vertical pixels via the upper-half-block
``▀`` (foreground = top pixel, background = bottom pixel), so a WxW pixel face fills
W columns x W/2 rows and reads roughly square. Degrades to a seed/traits text line
where the terminal lacks truecolor (and is a no-op-safe still frame for non-TTY/CI).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, Mapping, Optional

from modules.pfp.mesh import Mesh

_UPPER_HALF = "▀"
_RESET = "\x1b[0m"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_HOME = "\x1b[H"
_CLEAR = "\x1b[2J"


def supports_truecolor(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the terminal advertises 24-bit color via COLORTERM."""
    env = os.environ if env is None else env
    return env.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _as_mesh(config_or_mesh: Any) -> Mesh:
    return config_or_mesh if isinstance(config_or_mesh, Mesh) else Mesh(config_or_mesh)


def frame(config_or_mesh: Any, *, width: int = 48, t: float = 1.0,
          amp: float = 0.0, still: bool = False) -> str:
    """One rendered frame as a truecolor half-block string (no cursor control)."""
    mesh = _as_mesh(config_or_mesh)
    cols = width
    prow = width - (width % 2)          # even pixel-rows so they pair into half-blocks
    grid = mesh.grid(cols, prow, t=t, amp=amp, still=still)
    lines = []
    for rr in range(prow // 2):
        top = grid[2 * rr]
        bot = grid[2 * rr + 1]
        parts = []
        for c in range(cols):
            tr, tg, tb = top[c]
            br, bg, bb = bot[c]
            parts.append(f"\x1b[38;2;{tr};{tg};{tb};48;2;{br};{bg};{bb}m{_UPPER_HALF}")
        lines.append("".join(parts) + _RESET)
    return "\n".join(lines)


def text_line(config_or_mesh: Any) -> str:
    """The universal fallback: one line of seed + traits + voice."""
    mesh = _as_mesh(config_or_mesh)
    tr = mesh.traits()
    v = mesh.voice()
    return (
        f"seed {mesh.hex} · {tr['tier']} · head {tr['head']} · eyes {tr['eyes']} · "
        f"mouth {tr['mouth']} · antenna {tr['antenna']} · {tr['aura']} · "
        f"voice p{v['pitch']}·r{v['rate']}·t{v['timbre']}"
    )


def render(config_or_mesh: Any, *, width: int = 48, animate: bool = False,
           fps: int = 12, seconds: Optional[float] = None,
           out=None, env: Optional[Mapping[str, str]] = None) -> None:
    """Render to a stream. Picks the richest mode the terminal supports.

    - no truecolor / non-TTY  -> the seed/traits text line (always works)
    - truecolor + animate + TTY -> an animation loop (Ctrl-C or `seconds` to stop)
    - truecolor otherwise      -> a single still frame
    """
    out = sys.stdout if out is None else out
    mesh = _as_mesh(config_or_mesh)
    is_tty = bool(getattr(out, "isatty", lambda: False)())

    if not supports_truecolor(env) or not is_tty:
        print(text_line(mesh), file=out)
        return

    if not animate:
        print(frame(mesh, width=width, still=True), file=out)
        print(text_line(mesh), file=out)
        return

    delay = 1.0 / max(1, fps)
    deadline = None if seconds is None else time.monotonic() + seconds
    out.write(_HIDE_CURSOR + _CLEAR)
    try:
        t = 0.0
        while deadline is None or time.monotonic() < deadline:
            out.write(_HOME + frame(mesh, width=width, t=t, still=False) + "\n")
            out.write(text_line(mesh) + _RESET + "\n")
            out.flush()
            time.sleep(delay)
            t += delay
    except KeyboardInterrupt:
        pass
    finally:
        out.write(_SHOW_CURSOR + _RESET + "\n")
        out.flush()
