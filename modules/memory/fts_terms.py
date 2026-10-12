"""Pick the informative terms of a free-text recall query (B1, 2026-10-11).

The FTS5 recall query is an OR over at most ``MAX_TERMS`` quoted terms. Taking
the first 12 raw tokens of a long task spent the budget on boilerplate ("the",
"this", a repeated word), so the topic further on was never searched. Here
stopwords and case-insensitive duplicates are dropped first, order kept; a
query of ONLY stopwords falls back to its raw terms so a MATCH is never empty.
"""
from typing import List

MAX_TERMS = 12

#: Words that never pick a topic.
STOPWORDS = frozenset((
    "the and for are but not you your yours all any can had has have her him his how "
    "its our out was were who why what when where which while with this that these "
    "those they them their then than there here from into onto over under about after "
    "before again also just only very more most some such each both other same own "
    "will would could should shall may might must does did done doing been being "
    "let lets one two via per get got use used using now new it's i'm don't"
).split())


def select_terms(terms: List[str], limit: int = MAX_TERMS) -> List[str]:
    """At most ``limit`` terms: stopwords and duplicates out, first-seen order."""
    seen, picked = set(), []
    for t in terms:
        k = t.lower()
        if k in seen or k in STOPWORDS:
            continue
        seen.add(k)
        picked.append(t)
    return (picked or list(terms))[:limit]
