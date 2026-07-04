"""Task 20 (FL-D5/FL-D8, SK-F4): docs/CONFIGURATION.md flag hygiene + dead prompt-code removal.

Guards two cleanups from going stale:
- `CURATOR_LLM_MERGE` was removed from `agents/task/constants.py` (2026-06-29) but the
  doc row lingered — assert it's gone.
- Four flags added/wired since the last doc pass (`HMEM_TAIL_PLACEMENT`,
  `SKILL_OVERWRITE_PROTECT`, `INTERRUPT_REDIRECT`, `MEMORY_STORE_ANSWER_ONLY`) must have
  a doc row.
- `agents/task/agent/prompts.py`'s dead `PromptTemplates` class and `important_rules`
  method (zero callers) must stay deleted.
"""
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_configuration_doc_flag_hygiene():
    doc = (_REPO_ROOT / "docs" / "CONFIGURATION.md").read_text()
    assert "CURATOR_LLM_MERGE" not in doc  # FL-D5: removed flag
    for flag in ("HMEM_TAIL_PLACEMENT", "SKILL_OVERWRITE_PROTECT",
                 "INTERRUPT_REDIRECT", "MEMORY_STORE_ANSWER_ONLY"):
        assert flag in doc  # FL-D8


def test_dead_prompt_blocks_gone():
    src = (_REPO_ROOT / "agents" / "task" / "agent" / "prompts.py").read_text()
    assert "important_rules" not in src and "PromptTemplates" not in src
