"""Fixture pack ``echo`` (067 P2): the code half. Importing this module is
cheap; the tool and the CLI command are named by reference."""
from pathlib import Path

from core.packs.spec import PackSpec, ToolContribution


def echo_enabled() -> bool:
    return True


def pack() -> PackSpec:
    return PackSpec(
        id="echo",
        tools=(ToolContribution(id="echo", registrar="polyrob_echo.tool:register_echo_tool",
                                gate="polyrob_echo:echo_enabled"),),
        cli=("polyrob_echo.cli:echo",),
        skills_dir=Path(__file__).parent / "skills",
    )
