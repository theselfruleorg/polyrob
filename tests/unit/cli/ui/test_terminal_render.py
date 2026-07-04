"""Terminal render of the avatar mesh (cli/ui/terminal_render.py).

Renders the live field (modules/pfp/mesh) as truecolor half-blocks; degrades to a
seed/traits text line when the terminal lacks truecolor. No PNG involved.
"""
from cli.ui.terminal_render import (
    frame,
    text_line,
    supports_truecolor,
)
from modules.pfp.mesh import Mesh

CFG = {"generator": "mindprint@v2", "seed": "Rob Ottmachin", "variant": "", "override": {}}


def test_frame_has_half_the_width_in_char_rows():
    s = frame(CFG, width=48, still=True)
    lines = s.split("\n")
    assert len(lines) == 24  # 48 pixel rows -> 24 half-block char rows


def test_frame_uses_truecolor_half_blocks():
    s = frame(CFG, width=32, still=True)
    assert "▀" in s          # upper half block
    assert "\x1b[38;2;" in s      # 24-bit foreground (top pixel)
    assert ";48;2;" in s          # 24-bit background (bottom pixel, combined SGR)
    assert "\x1b[0m" in s         # reset


def test_frame_is_deterministic():
    assert frame(CFG, width=32, still=True) == frame(CFG, width=32, still=True)


def test_text_line_has_seed_traits_and_voice():
    line = text_line(Mesh(CFG))
    assert "0x" in line                 # seed hex
    assert "eyes" in line and "square" in line
    assert "voice" in line


def test_supports_truecolor_from_env():
    assert supports_truecolor({"COLORTERM": "truecolor"}) is True
    assert supports_truecolor({"COLORTERM": "24bit"}) is True
    assert supports_truecolor({}) is False
    assert supports_truecolor({"COLORTERM": "256"}) is False
