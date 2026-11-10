"""113 W4 item 10 rest / T-08: the words name the commands as they are now.

`run --resume` became `run --continue`, `polyrob skill` folds into
`polyrob skills` (the old name runs and the root help no longer prints
"related: skill"), and the surface hold is `polyrob surfaces pause|resume`.
"""
from pathlib import Path

from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]


def test_root_help_lists_skills_once_and_hides_the_old_name():
    from cli.polyrob import cli
    out = CliRunner().invoke(cli, ["--help"]).output
    rows = [line for line in out.splitlines() if "skill" in line]
    assert len(rows) == 1 and rows[0].strip().startswith("skills ")
    assert "related: skill" not in out
    assert cli.get_command(None, "skill") is not None  # the old name still runs


def test_no_stale_names_in_the_owner_text():
    stale = {
        "polyrob run --resume": ("cli/ui/commands/handlers.py", "docs/guide/getting-started.md",
                                 "docs/guide/conversation.md"),
        "polyrob surface pause": ("core/surfaces/circuit.py",),
        "polyrob surface resume": ("core/status_delivery.py", "core/surfaces/circuit.py"),
        "polyrob skill install": ("docs/guide/skills.md", "docs/guide/faq.md"),
        "skills (related: skill)": ("docs/guide/skills.md",),
    }
    for words, files in stale.items():
        for rel in files:
            assert words not in (ROOT / rel).read_text(encoding="utf-8"), f"{rel}: {words}"
    assert "--resume <id>" not in (ROOT / "docs/guide/getting-started.md").read_text()
