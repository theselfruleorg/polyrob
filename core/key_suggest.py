"""Closest-name suggestions for a setting key ("did you mean …?").

``difflib.get_close_matches`` alone ranks by character order, so a reordered
name loses to an unrelated one: prod 2026-10-10, ``preferences explain
TWITTER_APPROVAL_REQUIRED`` said only "unknown setting", and plain difflib picks
``APPROVAL_REQUIRED_TOOLS`` over the real ``TWITTER_REQUIRE_APPROVAL``. The score
here is the better of the character ratio and a WORD-overlap ratio (words split
on ``_ . -``; ``required`` ~ ``require``), so a name with the right words in the
wrong order still wins.

Pure and dependency-free: every "did you mean" seat (config service, prefs, the
agent's ``preferences`` action, the CLI) calls :func:`suggest_keys`.
"""
import difflib
import re
from typing import Iterable, List

_SPLIT = re.compile(r"[_.\-\s]+")


def _words(s: str) -> List[str]:
    return [w for w in _SPLIT.split(s.lower()) if w]


def _word_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def score(query: str, name: str) -> float:
    """0..1 — how likely ``name`` is the key the caller meant."""
    q, k = (query or "").strip(), (name or "").strip()
    if not q or not k:
        return 0.0
    char = difflib.SequenceMatcher(None, q.lower(), k.lower()).ratio()
    qw, kw = _words(q), _words(k)
    if not qw or not kw:
        return char
    hit = sum(1 for w in qw if any(_word_match(w, u) for u in kw))
    return max(char, hit / max(len(qw), len(kw)))


def suggest_keys(query: str, names: Iterable[str], *, n: int = 3,
                 cutoff: float = 0.6) -> List[str]:
    """Up to ``n`` names, best first, scoring at least ``cutoff``."""
    q = (query or "").lower()
    # Ties (same words) go to the closer spelling: goal.daily_quota ->
    # goals.daily_quota, not GOAL_DAILY_QUOTA.
    scored = sorted(
        ((score(query, nm), difflib.SequenceMatcher(None, q, nm.lower()).ratio(), nm)
         for nm in set(names or ())),
        key=lambda t: (-t[0], -t[1], t[2]))
    return [nm for s, _c, nm in scored if s >= cutoff and nm != query][:n]


def did_you_mean(query: str, names: Iterable[str], *, n: int = 3) -> str:
    """`` (did you mean A, B or C?)`` — or ``""`` when nothing is close."""
    hits = suggest_keys(query, names, n=n)
    if not hits:
        return ""
    if len(hits) == 1:
        return f" (did you mean {hits[0]}?)"
    return f" (did you mean {', '.join(hits[:-1])} or {hits[-1]}?)"


def flag_hint(query: str, err: str, pref_names: Iterable[str],
              flag_names: Iterable[str]) -> str:
    """A preference refusal's tail naming an instance FLAG that matches ``query``
    better than any preference does (e.g. TWITTER_REQUIRE_APPROVAL), with its
    seat — flags are not preferences. ``""`` when no flag is the closer match."""
    flag = suggest_keys(query, flag_names, n=1)
    pref = suggest_keys(query, pref_names, n=1)
    if not flag or (pref and score(query, flag[0]) <= score(query, pref[0])):
        return ""
    lead = " — or" if "did you mean" in (err or "") else " — did you mean"
    return (f"{lead} the instance flag {flag[0]}? Flags are not preferences; "
            f"the owner sets them (`polyrob config set`).")
