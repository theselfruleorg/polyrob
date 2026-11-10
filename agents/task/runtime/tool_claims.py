"""A text that says a registered tool does not exist (prod 2026-11-06).

Two runs told the owner "I have no file-write tool" and "X read/post tools
unavailable" with those tools in the request, loaded, and never tried. One run
then asked the owner to paste ledger lines by hand and stored the false reason as
the job's outcome. ``change_claims`` skips a negative sentence, so it cannot see
this. Here a send or a ``done`` text with such a sentence does not run, and the
model gets the exact tool names.

A claim about a tool whose last call failed is not refused: it can be true. The
caller refuses at most :data:`MAX_REFUSALS` times per run, so a wrong match
delays a message and never loses it.
"""
from __future__ import annotations  # OK here: this is NOT an action-registration module

import re
from typing import Iterable, List, Optional, Set

MAX_REFUSALS = 2

_ABSENT = re.compile(
    r"\bno\b|\bnot\b.{0,16}\b(?:available|loaded|registered|enabled)\b|"
    r"\b(?:don['’]t|do not|doesn['’]t|does not|didn['’]t|did not) have\b|\bhave no\b|\bhad no\b|"
    r"\black(?:s|ing)?\b|\bwithout\b|\bunavailable\b|\bmissing\b", re.I)
#: A tool name alone needs a statement about the tool: "I made no twitter_post" is a
#: report of what the run did, and it says nothing about the tool.
_ABSENT_NAMED = re.compile(
    r"\bnot\b.{0,16}\b(?:available|loaded|registered|enabled)\b|\bunavailable\b|\bmissing\b|"
    r"\b(?:don['’]t|do not|doesn['’]t|does not|didn['’]t|did not) have\b|\bhave no\b|\bhad no\b",
    re.I)
_TOOL_WORD = re.compile(r"\b(?:tools?|access)\b", re.I)
#: A capability the model names in words → the tools that give it.
_CAPABILITIES = (
    ("file write",
     re.compile(r"file[- ]?writ\w*|writ\w* (?:to )?(?:a |the |any )?files?|write[- ]?(?:tool|access)|"
                r"file(?:system)? tools?", re.I),
     ("filesystem_write_file", "filesystem_append_file", "filesystem_jsonl_append")),
    ("X read",
     re.compile(r"\b(?:x|twitter|tweet)[ /-]*read|\bread\w* (?:from |the )?(?:x|twitter|tweets?)\b", re.I),
     ("twitter_get_tweets", "twitter_get_timeline", "twitter_search")),
    ("X post",
     re.compile(r"\b(?:x|twitter|tweet)[ /-]*(?:read/)?post|\bpost\w* (?:to |on )?(?:x|twitter)\b", re.I),
     ("twitter_post", "twitter_quote", "twitter_reply")),
)
_SENTENCE = re.compile(r"(?<=[.!])\s+|\n+")


def falsely_absent(text: str, registered: Set[str], errored: Iterable[str] = ()) -> List[str]:
    """The registered tools that *text* says do not exist. ``[]`` when none."""
    failed = set(errored)
    out: List[str] = []
    for sentence in _SENTENCE.split(text or ""):
        if "?" in sentence or not _ABSENT.search(sentence):
            continue
        named = ([n for n in registered if "_" in n and n in sentence]
                 if _ABSENT_NAMED.search(sentence) else [])
        if _TOOL_WORD.search(sentence):
            for _label, words, tools in _CAPABILITIES:
                if words.search(sentence):
                    named.extend(t for t in tools if t in registered)
        for name in named:
            if name not in failed and name not in out:
                out.append(name)
    return out


def _step_texts(dump: list) -> Iterable[str]:
    """The words a step says to a person: its sends and its ``done`` text."""
    for action in dump:
        if not isinstance(action, dict):
            continue
        for name, params in action.items():
            if not isinstance(params, dict):
                continue
            if name in ("send_message", "done") or (
                    name == "message" and params.get("action") in (None, "send")):
                yield str(params.get("text") or "")


def refusal_for_step(dump: list, registered: Set[str], errored: Iterable[str] = ()) -> Optional[str]:
    """The error for a step whose text denies a registered tool, else None."""
    errored = set(errored)
    tools: List[str] = []
    for text in _step_texts(dump):
        tools.extend(t for t in falsely_absent(text, registered, errored) if t not in tools)
    if not tools:
        return None
    return ("NOT SENT: your text says a tool does not exist, but these tools are registered "
            f"and loaded in this run: {', '.join(sorted(tools))}. Call the tool now. If the "
            "call fails, report the error it returns.")
