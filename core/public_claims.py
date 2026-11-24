"""The post-claim guard (order 0023): a public post states an act only with its receipt.

The owner's rule, in code: an agent never posts what has not happened. Text the
agent did not write (a page, a goal's failure text, an inbox item) is DATA — this
module never reads such text as an instruction; it only classifies the agent's
OWN outbound post text and the tool results of the current turn.

The rule, applied before a ``social``-effect verb runs
(``tools.controller.effect_hooks.make_claim_guard_hook``):

* a **done** claim — a past-tense act word (``minted`` · ``funded`` ·
  ``traded`` · ``sold``) — passes only when the post NAMES a transaction hash
  (``0x`` + 64 hex) that a tool result of the CURRENT turn shows LANDED. A hash
  the turn did not read is a claim from memory, which is exactly the failure
  mode; a hash whose result carries a not-landed marker (pending, failed,
  reverted, …) is not evidence; a hash inside the untrusted-result envelope
  (a timeline read, a fetched page) is a third party's transaction, not ours.
* a **future** claim — a future marker (``will``, ``going to``, ``plans to``, …)
  before an act verb, or an act-``-ing`` form before a time word — is refused
  outright: an act that has not happened is never announced, whatever the
  evidence says.
* Everything else passes. There is no exemption, not for the owner's own turn:
  the remedy is always the same honest loop — read the receipt this turn, then
  post naming its hash.

Deliberate limits (stated in the docs, not hidden): the classifier reads act
VERBS, not bare nouns (``the sale opens Friday`` is a schedule the skills
forbid; it is not caught here); the hash shape is EVM (``0x`` + 64 hex) — a
Solana signature is not matched; "this turn" is the current session's messages
after the last genuine user turn.
"""
import re
from typing import FrozenSet, Iterable, Optional, Tuple

#: (kind, word) — kind is ``"done"`` or ``"future"``; ``word`` is the act word found.
Finding = Tuple[str, str]

#: The act verbs of the order (mint · fund · trade · sale) in every inflection
#: that can follow a future marker.
_ACT_VERB = r"mint(?:s|ed|ing)?|fund(?:s|ed|ing)?|trade(?:s|d|ing)?|sell(?:s|ing)?|sold"

#: Past-tense/participle forms only: an unambiguous statement that the act
#: happened. A hyphen compound ("well-funded", "state-funded") is an adjective,
#: not the agent's act claim.
_DONE_RE = re.compile(r"(?<!-)\b(minted|funded|traded|sold)\b", re.IGNORECASE)

#: A future marker within one clause (no sentence end between) of an act verb.
_FUTURE_RE = re.compile(
    r"\b(?:will|shall|going\s+to|gonna|about\s+to|plans?\s+to|planning\s+to|"
    r"prep(?:are[sd]?)?\s+to|preparing\s+to|intends?\s+to)"
    r"[^.!?;]{0,40}?\b(" + _ACT_VERB + r")\b",
    re.IGNORECASE)

#: An act in progress, as a VERB (an auxiliary before it, so "trading volume"
#: and "selling pressure" — the -ing form as a noun modifier — are not claims),
#: bound to a future-bound time word. "today" is deliberately absent: it names
#: the present, and it sits in ordinary market chat ("trading is up today").
_FUTURE_SOON_RE = re.compile(
    r"\b(?:is|are|was|were|am|be|being|been)\s+(?:now\s+|still\s+|already\s+)?"
    r"(minting|funding|trading|selling)\b[^.!?;]{0,30}?"
    r"\b(soon|tomorrow|tonight|later|shortly|next\s+(?:week|month)|this\s+week|"
    r"in\s+\d+\s*(?:min|minute|hour|day|week)s?)\b",
    re.IGNORECASE)

#: An EVM transaction hash, the one receipt shape this guard reads.
_HASH_RE = re.compile(r"\b0x[0-9a-fA-F]{64}\b")

#: A result that names a hash AND one of these did NOT show it landed — a
#: broadcast, a wait, a simulation. Only a message with no marker at all is
#: landed evidence: the guard errs toward refusing, never toward vouching.
_NOT_LANDED_RE = re.compile(
    r"\b(pending|unconfirmed|not\s+confirmed|may\s+still\s+land|failed|reverted|"
    r"in_flight|dropped|not\s+mined|submitted|broadcast(?:ing)?|awaiting\s+"
    r"confirmation|simulat(?:ed|ion)|error)\b",
    re.IGNORECASE)

#: The untrusted-result envelope (``core/security/untrusted_wrap.py``): a tool
#: result that carries third-party content (a timeline read, a fetched page) is
#: framed with this marker on the native tool-call path. A hash inside it is
#: SOMEBODY ELSE'S receipt — it never vouches for our act.
_UNTRUSTED_ENVELOPE_RE = re.compile(r"<\s*untrusted_tool_result", re.IGNORECASE)


def claim_finding(text: str) -> Optional[Finding]:
    """The act claim in *text*, or None when it makes none.

    A future claim answers before a done claim (``will have minted`` announces,
    it does not report); within a kind the FIRST match answers.
    """
    text = str(text or "")
    if not text.strip():
        return None
    match = _FUTURE_RE.search(text) or _FUTURE_SOON_RE.search(text)
    if match:
        return ("future", match.group(1).lower())
    match = _DONE_RE.search(text)
    if match:
        return ("done", match.group(1).lower())
    return None


def hashes_in_text(text: str) -> FrozenSet[str]:
    """Every EVM transaction hash in *text*, lowercased."""
    return frozenset(h.lower() for h in _HASH_RE.findall(str(text or "")))


def landed_hashes(result_texts: Iterable[str]) -> FrozenSet[str]:
    """The hashes the tool-result texts show LANDED.

    A text that carries a not-landed marker vouches for nothing: a pending or
    failed receipt is not evidence that an act happened. A text inside the
    untrusted-result envelope is third-party content (a timeline read, a page):
    a hash there is somebody else's transaction, never our receipt.
    """
    out: FrozenSet[str] = frozenset()
    for raw in result_texts or ():
        text = str(raw or "")
        found = hashes_in_text(text)
        if (found and not _NOT_LANDED_RE.search(text)
                and not _UNTRUSTED_ENVELOPE_RE.search(text)):
            out |= found
    return out


def post_claim_refusal(post_text: str,
                       turn_result_texts: Optional[Iterable[str]]) -> Optional[Finding]:
    """The finding a public post must be refused for, or None to let it pass.

    *turn_result_texts* is the current turn's tool-result texts, or None when
    they could not be read — a done claim then refuses (fail closed), ordinary
    text never does.
    """
    finding = claim_finding(post_text)
    if finding is None:
        return None
    if finding[0] == "future":
        return finding
    evidence = landed_hashes(turn_result_texts or ())
    return None if hashes_in_text(post_text) & evidence else finding


__all__ = ["Finding", "claim_finding", "hashes_in_text", "landed_hashes",
           "post_claim_refusal"]
