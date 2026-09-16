"""Is this text a bookkeeping recap of what was already said? (contract C6).

The belt to :mod:`core.surfaces.turn_reply`'s braces. The latch stops the
framework from PUBLISHING a second final message; this catches the case where a
recap arrives through some other path (a renderer's turn-end hook, a surface
that assembles its own final block, a legacy caller).

It used to live in ``cli/ui/dialog.py`` — one renderer — so Telegram, the
webview console, email and every other seat had nothing (finding F3). Its verb
list also lacked ``answered|informed|updated|summarised`` and it capped at 200
characters, so the ~600-character recap actually observed would have slipped
past the single seat that did have the fix.

Conservative by construction: when unsure, return False. Eating a genuine answer
is a far worse failure than showing one redundant bubble, and this runs on the
path that carries real user-facing text.
"""
import re
from typing import Optional

#: Bookkeeping narration about the turn that just happened. Widened from the
#: cli/ui original, which knew nothing of "answered"/"informed"/"updated".
_RECAP_LEAD_RE = re.compile(
    r"^(responded|response|sent|greeted|replied|reported|acknowledged|"
    r"answered|informed|updated|summaris(?:ed|ing)|summariz(?:ed|ing)|notified|"
    r"confirmed|shared|relayed|delivered|provided|completed|task complete|done|"
    r"finished|no further|"
    r"i (?:have )?(?:responded|replied|greeted|sent|reported|answered|informed|"
    r"updated|notified|confirmed|shared))\b",
    re.IGNORECASE,
)

#: Ceiling for lead-verb narration. The old 200 excluded the real leak; 1200 is
#: still short enough that a genuine long-form answer (a report, a poem, code)
#: is judged by the overlap rule below rather than by its opening word.
_MAX_NARRATION_CHARS = 1200

#: Fraction of an answer's meaningful words that must already appear in the
#: bubble before it counts as a restatement. High on purpose.
_OVERLAP_RATIO = 0.85

_WORD_RE = re.compile(r"[a-z0-9]{3,}")

#: Words that carry no topical signal, so they must not inflate the overlap.
_STOPWORDS = frozenset(
    "the and for with that this from was were has have had are you your not "
    "but all any can its it's their they them then than there here what when "
    "which who whom will would should could been being does did done".split()
)


def _content_words(text: str) -> set:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def is_redundant_recap(answer: Optional[str], bubble_text: Optional[str]) -> bool:
    """True when *answer* merely re-reports *bubble_text* rather than adding to it.

    Three ways to qualify, in order of confidence:

    1. it is byte-identical to what was already shown;
    2. it opens with bookkeeping narration ("Answered the owner's question…")
       and is short enough to be narration rather than substance;
    3. it introduces no content word the bubble did not already carry.
    """
    a = (answer or "").strip()
    b = (bubble_text or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) <= _MAX_NARRATION_CHARS and _RECAP_LEAD_RE.match(a):
        return True
    # A restatement says nothing new. Require the answer to be no longer than the
    # bubble as well: a longer text sharing the bubble's vocabulary is usually an
    # elaboration (which the user wants), not a recap.
    a_words = _content_words(a)
    if not a_words or len(a) > len(b):
        return False
    b_words = _content_words(b)
    if not b_words:
        return False
    shared = len(a_words & b_words) / len(a_words)
    return shared >= _OVERLAP_RATIO
