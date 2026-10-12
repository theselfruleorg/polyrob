"""What a cross-session ``memories`` row must NOT hold (B2 write quality).

Prod (2026-10-11, 25,437 rows): about four rows in five were transient chatter,
not findings — ~3,800 ``[STRATEGIC CLUE FROM …]`` phase hints, ~2,000
``tool(args)→result`` echoes from the action-summary fallback, ~3,000 run-status
lines ("Starting the 02:20Z safety monitor run. Plan: …", "DM CHECK complete …
nothing new"). Recall ranked them like lessons.

This is the ONE classifier for that noise. It runs at the write seam
(``SqliteMemoryProvider._compose_stored_content``, shared by the keyword and
vector halves, so both store the same string) and drives the one-time purge of
the rows already stored (``modules.memory.retention.purge_noise_rows``).

It is deliberately CONSERVATIVE: a line is dropped only when it matches a
narrow noise shape, and a run-status line that carries an owner ruling, a
lesson or a decision is kept. When in doubt, keep.

Pure functions, no I/O, stdlib only.
"""
import re
from typing import List, Optional, Tuple

#: Bump when the rules change so the stored-row purge re-runs once.
CLASSIFIER_VERSION = 3

_UNVERIFIED = "[unverified] "

# ``name(args)→result`` / ``name→result`` — the action-summary fallback
# (``MemoryWriterMixin._build_memory_from_actions``). A lower-case snake id
# immediately followed by an optional arg list and the arrow.
_ECHO_RE = re.compile(r"^[a-z][a-z0-9_]{2,}(?:\([^\n]{0,400}?\))?\s?→")
# ``Found: <raw tool output>`` — the results fallback
# (``_extract_finding_from_results``) — is an echo too.
_FOUND_RE = re.compile(r"^Found: ")

# ``[STRATEGIC CLUE FROM DISCOVERY] Discovery: 5 insights. Proceed with …`` —
# the phase-transition hint (``modules/memory/task/phase_manager.py``). The
# generator always writes it as ONE line of boilerplate (counts + a fixed
# instruction); the finding after it in a stored row is on the NEXT line. The
# drain stopped writing it on 2026-10-03; the rows stayed.
_CLUE_PREFIX = "[STRATEGIC CLUE FROM "

# A line that opens a new finding (ends an absorbed echo block): a capitalised
# word, then prose. Raw tool output rarely looks like this at >= 60 chars.
_FINDING_START_RE = re.compile(r"^[A-Z][A-Za-z'’-]+[ :,]")
_OUTPUT_START_RE = re.compile(
    r"^(?:\[stderr\]|\[stdout\]|Error:|[A-Za-z]*Error\b|Bundling\b|Traceback\b|\s|\d|[/.#|>{\[(\"'-])")

# Run-status narration.
_START_RE = re.compile(
    r"^(?:Starting|Beginning|Kicking off|Begin)\b[^\n]{0,120}?"
    r"\b(?:run|slot|round|pass|check|cycle|monitor|rail|tick|sweep|watch(?:er)?|job)\b",
    re.IGNORECASE)
_PLANNING_RE = re.compile(r"^Planning turn done\b", re.IGNORECASE)
_BARE_COMPLETE_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}Z?\s+)?(?:the\s+)?(?:[A-Za-z-]+\s+){0,3}"
    r"(?:run|check|round|pass|job|task|cycle|sweep)\s+(?:is\s+)?"
    r"(?:complete|completed|done|finished)\b[.!]?\s*$",
    re.IGNORECASE)
_DONE_RE = re.compile(r"\b(?:complete|completed|done|finished|closed|over)\b", re.IGNORECASE)
_NOTHING_RE = re.compile(
    r"\b(?:nothing new|no new (?:thread|inbound|message|dm|mention|item|reply|replies|activity|event)s?"
    r"|no changes?|unchanged|silent no-op|no-op|no action (?:needed|possible|taken)"
    r"|nothing to (?:do|report|redo|act on)|no pending work|zero new)\b",
    re.IGNORECASE)

# What makes a status line worth keeping anyway: an owner ruling, a lesson, a
# decision, a root cause. Narrow on purpose — "owner" alone is not enough (every
# run plan mentions whether it will message the owner).
_PROTECT_RE = re.compile(
    r"\b(?:rul(?:ed|ing)|lessons?|learned|learnt|root cause|mistakes?|decid\w*|decisions?"
    r"|declin\w*|approv\w*|reject\w*|standing rule|prefer\w*|polic(?:y|ies)"
    r"|rules?|from now on|going forward|must not|never again|correct(?:ion|ed)"
    r"|owner (?:said|says|asked|asks|wants|wrote|answered|replied|confirmed|clarified"
    r"|instructed|told|ruled))\b"
    r"|^(?:Owner|User requested|User said)\b",
    re.IGNORECASE)


# Narration rules apply only to SHORT lines that carry no measurement: a long
# line or one with an amount, a decimal, a percentage, a large number, an
# address or a tx hash is a record of what was measured (review of v1: 853
# such prod lines were dropped).
_STATUS_MAX_CHARS = 200
_DATA_RE = re.compile(
    r"0x[0-9a-fA-F]{6,}|[$€£]\s?\d|\d\.\d|\d{1,3}(?:,\d{3})+|\d\s?%|\b\d{4,}\b(?!-\d{2}-)")


# A "done, nothing changed" line that still explains a failure or a refusal is
# a diagnosis, not chatter.
_DIAGNOSIS_RE = re.compile(
    r"\b(?:because|refus\w*|block\w*|fail\w*|errors?|bugs?|broken|wrong|instead"
    r"|missing|stuck|cannot|can't)\b",
    re.IGNORECASE)


def _body(line: str) -> str:
    return line[len(_UNVERIFIED):] if line.startswith(_UNVERIFIED) else line


def is_echo_line(line: str) -> bool:
    """True for a ``tool(args)→result`` or ``Found: …`` echo line."""
    b = _body(line)
    return bool(_ECHO_RE.match(b) or _FOUND_RE.match(b))


def is_clue_line(line: str) -> bool:
    """True for a ``[STRATEGIC CLUE FROM …]`` phase-hint line."""
    return _body(line).startswith(_CLUE_PREFIX)


def is_protected(text: str) -> bool:
    """True when *text* carries an owner ruling, a lesson or a decision —
    never noise, and never aged out on the short autonomous window."""
    return any(_PROTECT_RE.search(_body(line).strip()) for line in (text or "").split("\n"))


def status_noise_reason(line: str) -> Optional[str]:
    """Why a single (non-echo) line is run-status chatter, or None."""
    b = _body(line).strip()
    if not b or _PROTECT_RE.search(b):
        return None
    if len(b) > _STATUS_MAX_CHARS or _DATA_RE.search(b):
        return None
    if _PLANNING_RE.match(b) or _START_RE.match(b):
        return "status"
    if _BARE_COMPLETE_RE.match(b):
        return "status"
    if _DONE_RE.search(b) and _NOTHING_RE.search(b) and not _DIAGNOSIS_RE.search(b):
        return "status"
    return None


def _starts_new_finding(line: str) -> bool:
    b = _body(line)
    if is_echo_line(line) or is_clue_line(line):
        return True
    if _OUTPUT_START_RE.match(b):
        return False
    # A short finding-shaped line with an owner ruling or a measurement ends the
    # echo block ("Owner wants daily digest at 9.", "PNL buyback filled at 0.05
    # ETH."); the purge must not delete it as tool output (review 2026-10-11).
    if _FINDING_START_RE.match(b) and (_PROTECT_RE.search(b) or _DATA_RE.search(b)):
        return True
    if len(b) < 60:
        return False
    return bool(_FINDING_START_RE.match(b))


def filter_content(content: str) -> Tuple[str, List[str]]:
    """Drop the noise lines of a stored-row body. Returns ``(kept, reasons)``.

    Line-based (the step drain joins findings with ``\\n``). An echo line opens
    an echo block that also swallows the tool-output lines after it, up to the
    next line that looks like a new finding. A clue line is dropped whole.
    ``kept`` is '' when nothing worth storing is left.
    """
    if not content:
        return "", []
    kept: List[str] = []
    reasons: List[str] = []
    in_echo = False
    for line in content.split("\n"):
        if in_echo and not _starts_new_finding(line):
            continue  # tool output that belongs to the echo above
        in_echo = False
        if is_echo_line(line):
            in_echo = True
            reasons.append("echo")
            continue
        if is_clue_line(line):
            reasons.append("clue")
            continue
        why = status_noise_reason(line) if line.strip() else None
        if why:
            reasons.append(why)
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    return out, reasons


def clean(content: str) -> str:
    """``filter_content`` without the reasons."""
    return filter_content(content)[0]
