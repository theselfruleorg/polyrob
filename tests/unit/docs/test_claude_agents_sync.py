import re
from pathlib import Path


CLAUDE_MD = Path("CLAUDE.md")
AGENTS_MD = Path("AGENTS.md")

# ROB_LOCAL not immediately preceded by POLY — catches a regression where AGENTS.md
# named this flag ROB_LOCAL while the code (and every other doc) reads POLYROB_LOCAL.
ROB_LOCAL_BARE = re.compile(r"(?<!POLY)ROB_LOCAL\b")


def test_claude_md_stays_a_thin_pointer():
    """CLAUDE.md must stay a minimal pointer to AGENTS.md, not re-accumulate content.

    Tools that don't resolve the `@AGENTS.md` import (or read CLAUDE.md directly, e.g.
    agents/task/agent/core/project_context.py which picks AGENTS.md over CLAUDE.md and
    does not concatenate them) get ONLY this file's content.
    """
    text = CLAUDE_MD.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 15, (
        "CLAUDE.md grew beyond a thin pointer — move new architecture content into AGENTS.md"
    )
    assert "AGENTS.md" in text


def test_agents_md_is_the_resolved_canonical_doc():
    """AGENTS.md must exist and stay substantial — CLAUDE.md points readers to it."""
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert len(text.splitlines()) > 200


def test_agents_md_does_not_use_stale_rob_local_name():
    assert not ROB_LOCAL_BARE.findall(AGENTS_MD.read_text(encoding="utf-8"))
