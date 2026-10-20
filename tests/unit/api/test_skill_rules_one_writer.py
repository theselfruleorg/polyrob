"""A tenant ``rules.json`` has ONE parser and ONE atomic writer, on the skill
writer (``modules/skills/writer.py``). The REST skill API had its own
``open(..., "w")`` writer (non-atomic) and its own reader."""
from pathlib import Path


def test_the_rest_api_reads_and_writes_rules_through_the_skill_writer():
    src = Path("api/skill_endpoints.py").read_text()
    assert "open(rules_file" not in src and "user_dir / \"rules.json\"" not in src
    assert "_save_user_rules" in src and "_load_user_rules" in src


def test_the_writer_has_one_rules_write():
    src = Path("modules/skills/writer.py").read_text()
    assert src.count("self._atomic_write(self._user_rules_path(") == 1
