"""Regression guard: the CLI's user-facing brand is `polyrob`, not the legacy `rob`.

The binary is `polyrob` (pyproject [project.scripts]). A half-finished rename left
`[rob]` diagnostic prefixes + `rob <cmd>` copy-paste hints that print a name with no
matching executable. This guards the prefix (the unambiguous signal) from drifting
back. NOTE: instance-identity labels (the agent's display name) are intentionally the
INSTANCE name and are handled separately — they are not `[rob]`-prefixed.
"""
import pathlib


def _cli_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3] / "cli"


def test_no_legacy_rob_bracket_prefix_in_cli():
    offenders = []
    for f in _cli_root().rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "[rob]" in line:
                offenders.append(f"{f.relative_to(_cli_root().parent)}:{i}")
    assert not offenders, f"legacy [rob] prefix (use [polyrob]): {offenders}"
