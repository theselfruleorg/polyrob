"""The Inbox — one composed list of what is waiting on a person (043 §1.3).

**The definition, and it is a definition with teeth.** An Inbox item is

    a durable record, for this tenant, blocked on an owner decision,
    that Rob cannot resolve alone.

All four tests must hold. The two that are easiest to lose are the last two:

* A late invoice is a *durable record for this tenant*, but the settlement
  watcher closes it on its own, so it is **not** a decision. It is listed under
  ``not_blocking`` and it is **not counted**. A badge that counts things nobody
  has to decide is a badge people learn to ignore.
* A source that could not be read is **an entry with its own name**, never an
  omission and never a zero. ``count`` then becomes a FLOOR, ``uncertain`` is
  true, and every seat must say so. "Nothing needs you" is only sayable when
  every source answered.

**Why this lives in ``core/``.** Two seats render it — the console's Inbox page
and the REPL's ``/inbox`` — and neither may import the other. So the composition
is pure and the collectors are injected: :func:`build_inbox` takes a mapping of
``name -> callable(user_id) -> list[Item]``. The concrete readers (the goal
board, the app registry, the self-evolution queue, …) live one tier up in
``surfaces/inbox_sources.py``, which is allowed to reach ``tools`` and ``agents``.

A collector signals "I could not read my store" by **raising**. Returning ``[]``
means "I read it, and it holds nothing" — the two are different answers and the
whole module exists to keep them different.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: What a seat may ask an item to do. Deliberately a small, seat-neutral
#: vocabulary: the console draws these as buttons and the REPL prints them as
#: commands, so neither seat's words leak into the other's. An item-specific
#: phrasing ("Put it online", "Keep it") is the SEAT's job, not this module's.
ACTIONS = ("approve", "reject", "fulfill", "show")

#: Every source the Inbox reads, in read order, with the words a person sees
#: when it refuses. Lower-case because these appear mid-sentence ("I could not
#: read the spend approvals"), never as a heading.
SOURCE_LABELS: Dict[str, str] = {
    "self_evolution": "my own proposals",
    "tool_approvals": "spend approvals",
    "correspondents": "correspondents",
    "asks": "blocked goals",
    "apps": "apps",
}

#: The marker kind a refused source contributes. A seat renders it with its own
#: dashed rule; it is never a decision and never counted.
KIND_UNREADABLE = "unreadable"


@dataclass
class Item:
    """One row of the Inbox.

    ``blocking`` is the question "does Rob stop until a person answers". It is
    what ``count`` counts, and it is the only thing that does.

    ``expires_at``/``created_at`` are epoch seconds; ``None``/``0.0`` mean the
    record carries no such stamp, which is common — a quarantined proposal has
    no deadline and, on disk, no reliable age.
    """

    kind: str
    id: str
    title: str = ""
    body: str = ""
    meta: str = ""
    blocking: bool = True
    expires_at: Optional[float] = None
    created_at: float = 0.0
    actions: Tuple[str, ...] = ()
    source: str = ""
    unreadable: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "id": str(self.id),
            "title": self.title,
            "body": self.body,
            "meta": self.meta,
            "blocking": bool(self.blocking),
            "expires_at": self.expires_at,
            "created_at": float(self.created_at or 0.0),
            "actions": list(self.actions),
            "source": self.source,
            "unreadable": bool(self.unreadable),
            "extra": dict(self.extra),
        }


def unreadable_item(source: str) -> Item:
    """The entry a refused source contributes.

    It is BLOCKING (it sits in the main list, where a person will see it) and
    never counted (nobody can decide a store that did not answer).
    """
    return Item(kind=KIND_UNREADABLE, id=source,
                title=SOURCE_LABELS.get(source, source),
                blocking=True, unreadable=True, source=source,
                actions=("show",))


_FAR_FUTURE = float("inf")


def _sort_key(item: Item):
    """Blocking, then expiring soonest, then oldest.

    Age is the tie-break, not the sort key: an item with a deadline outranks
    every item without one, because the deadline is the thing that can be
    missed. An unreadable entry has neither a deadline nor a real age, so it
    sorts after the dated decisions rather than pretending to be the oldest of
    them (``created_at`` would otherwise make a source with no timestamp lead
    the whole list).
    """
    return (
        0 if item.blocking else 1,
        1 if item.unreadable else 0,
        _FAR_FUTURE if item.expires_at is None else float(item.expires_at),
        float(item.created_at or 0.0),
    )


def compose(items: Sequence[Item],
            sources: Mapping[str, str]) -> Dict[str, Any]:
    """Partition, order and count *items*; report *sources* verbatim.

    Returns the ONE body every seat renders::

        {"items": [...], "not_blocking": [...], "count": int,
         "uncertain": bool, "sources": {...}, "unreadable_sources": [...]}

    ``count`` is decisions only. ``uncertain`` is true when ANY source refused
    or any item is an unreadable marker — which is what makes the badge read
    ``1+`` and stops every seat from saying "nothing needs you".
    """
    for item in items:
        bad = [a for a in item.actions if a not in ACTIONS]
        if bad:
            raise ValueError(
                f"{item.kind}:{item.id} asks for actions outside the "
                f"vocabulary {ACTIONS}: {bad}")

    ordered = sorted(items, key=_sort_key)
    blocking = [i for i in ordered if i.blocking]
    listed = [i for i in ordered if not i.blocking]
    unreadable_sources = sorted(
        name for name, state in (sources or {}).items()
        if str(state).startswith("unreadable("))
    uncertain = bool(unreadable_sources) or any(i.unreadable for i in ordered)
    return {
        "items": [i.as_dict() for i in blocking],
        "not_blocking": [i.as_dict() for i in listed],
        "count": sum(1 for i in blocking if not i.unreadable),
        "uncertain": uncertain,
        "sources": dict(sources or {}),
        "unreadable_sources": unreadable_sources,
    }


def why(exc: BaseException) -> str:
    """The short reason a source refused, for ``unreadable(<why>)``.

    Bounded and single-line: this reaches a page and a terminal, and a store's
    exception text is not a place to trust for length or newlines.
    """
    text = f"{type(exc).__name__}: {exc}".strip()
    return " ".join(text.split())[:160]


def build_inbox(user_id: str,
                collectors: Mapping[str, Callable[[str], Any]]) -> Dict[str, Any]:
    """Run every collector for *user_id* and compose one Inbox body.

    A collector that raises becomes ``sources[name] = "unreadable(<why>)"`` AND
    an entry in the list. It never becomes an omission, and it never stops the
    collectors after it from contributing — one locked store must not lose the
    items the others returned.
    """
    items: List[Item] = []
    sources: Dict[str, str] = {}
    for name, collect in (collectors or {}).items():
        try:
            got = collect(user_id)
        except Exception as exc:  # noqa: BLE001 — a refusal is DATA here
            sources[name] = f"unreadable({why(exc)})"
            items.append(unreadable_item(name))
            continue
        sources[name] = "ok"
        items.extend(got or [])
    return compose(items, sources)
