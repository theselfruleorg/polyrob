"""Knowledge-reach ratchet (WS-K1, 2026-09-22).

The finding this holds the line on: on 2026-09-22 the status snapshot had
nineteen sections and not one of them was about what the agent KNOWS. 19,001
recall rows, 1,836 episodes, 76 indexed sources, 473 registered documents and
2,449 session trees were reported on no seat at all.

Two assertions, both cheap:

1. ``knowledge`` is a section of the ONE snapshot and every seat can title it.
   (``SECTION_ORDER`` is the aggregation gate — a section missing from it
   contributes no health item and reports no unavailability.)
2. Every knowledge STORE answers. The section reports one entry per store even
   when the file is absent, so a store that is added to the knowledge layer and
   not reported here fails this test rather than going quietly invisible.

Mirrors ``tests/test_nav_ratchet.py``: a count and a named set, not a snapshot
of prose.
"""
from core.status_knowledge import knowledge_section
from core.status_render import _SECTION_TITLES
from core.status_snapshot import SECTION_ORDER

#: One entry per store the knowledge layer owns. Adding a store means adding a
#: reader AND a name here — that is the whole point of the ratchet.
KNOWLEDGE_STORES = frozenset({
    "recall",      # memory.db::memories
    "curated",     # memory.db::curated_memory
    "episodes",    # memory.db::episodes
    "kb",          # memory.db::kb_sources
    "documents",   # artifacts.db
    "skills",      # skill_usage.db
    "sessions",    # the session tree
})


def test_knowledge_is_a_section_of_the_one_snapshot():
    assert "knowledge" in SECTION_ORDER, (
        "a section absent from SECTION_ORDER contributes no health item and "
        "reports no unavailability — it is invisible where it matters"
    )
    assert _SECTION_TITLES.get("knowledge"), (
        "every section needs a title or the generic renderer prints its key"
    )


def test_every_knowledge_store_reports_even_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "sessions"))
    sec = knowledge_section("rob", str(tmp_path))

    assert set(sec.data) == KNOWLEDGE_STORES, (
        f"stores reported {sorted(sec.data)} vs expected {sorted(KNOWLEDGE_STORES)} "
        "— a store that does not answer here is reported on no seat"
    )
    # An absent store is not a defect and is never a zero.
    assert sec.health == []
    assert all(v == {"state": "absent"} for v in sec.data.values())
