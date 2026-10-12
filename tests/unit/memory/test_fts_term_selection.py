"""B1: the FTS recall query picks informative terms, not just the first 12 tokens.

Prod: a long task spent its 12-term budget on boilerplate ("the", "this", "they",
repeated words), so recall never searched on the topic at the end of the task.
"""
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider as P


def _terms(match: str) -> list:
    return [t.strip('"') for t in match.split(" OR ")]


def test_stopwords_and_duplicates_are_dropped():
    q = ("the job that they wrote and the job that they ran with this and that "
         "for the buyback swap")
    picked = _terms(P._fts_match(P._query_terms(q)))
    assert "buyback" in picked and "swap" in picked
    assert "the" not in picked and "that" not in picked
    assert len(picked) == len({t.lower() for t in picked})


def test_at_most_twelve_terms():
    q = " ".join(f"topic{i}" for i in range(30))
    assert len(_terms(P._fts_match(P._query_terms(q)))) == 12


def test_all_stopwords_falls_back_to_the_raw_terms():
    picked = _terms(P._fts_match(P._query_terms("what was this about")))
    assert picked  # never an empty MATCH
