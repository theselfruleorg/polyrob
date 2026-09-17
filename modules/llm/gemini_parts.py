"""Gemini response-part predicates (extracted from gemini_client.py, 2026-09-18).

A proto-plus ``Part`` exposes EVERY oneof member as an attribute, so
``hasattr(part, "function_call")`` is True for a plain text part too (its
``function_call`` is an empty message with ``name == ""``). Until 2026-09-17
the client took that branch for every text part, logged an ERROR and
``continue``d past it — every text-only Gemini reply was DROPPED as "empty
action list" and the model was pushed into a filler step. Presence must be
asked of the oneof via ``in``.
"""


def part_is_function_call(part) -> bool:
    """True only when the Part's ``function_call`` oneof member is SET.

    A duck-typed double without ``__contains__`` falls back to "the attribute
    exists and is not None".
    """
    try:
        return "function_call" in part
    except TypeError:
        return getattr(part, "function_call", None) is not None
