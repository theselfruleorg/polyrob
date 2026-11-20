"""approval_target — what the owner typed or tapped, mapped to ONE pending item.

The ONE resolver every seat's ``/approve`` and ``/reject`` uses (proposal 125 S10):
the REPL, ``polyrob owner approve``, Telegram and the console. Before it, the REPL
matched the bare id itself, so an id two kinds share decided whichever row came
first, ``kind:id`` was refused, and a tapped ``tap-<id>`` that had left the open
queue answered "no pending item" instead of the tool-approval lane's own answer.

- :func:`resolve_pending_target` — the open-queue match (alias, id, ``kind:id``,
  ``<id>@<rev>``). ``None`` for no match AND for more than one.
- :func:`resolve_decision_target` — the same seam plus the two decision rules:
  an ambiguous id is NAMED (never guessed), and a gone ``tap-<id>`` goes to the
  ``tool_approval`` lane.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

#: The display prefix of a queued ``tool_approval`` ask. ⚠️ This module imports
#: nothing from ``approval_queue`` (which re-exports these names): that import
#: would pull the resolver into the tools/cron import cycle.
TAP_PREFIX = "tap-"


def tap_display_id(ask_id: str) -> str:
    """The namespaced id a `tool_approval` ask is SHOWN as (Telegram `/pending`,
    `polyrob owner pending`) — disambiguates from a self-evolution proposal id in
    the single-id `/approve <id>` / `/reject <id>` dispatch (no explicit `kind`)."""
    return f"{TAP_PREFIX}{ask_id}"


def strip_tap_prefix(display_id: str) -> Optional[str]:
    """Inverse of :func:`tap_display_id`. None when `display_id` isn't tap-prefixed."""
    if isinstance(display_id, str) and display_id.startswith(TAP_PREFIX):
        real = display_id[len(TAP_PREFIX):]
        return real or None
    return None


def resolve_pending_target(target: str, pending: Any) -> Optional[Dict[str, Any]]:
    """Map what the owner tapped or typed back to ONE item of the live queue.

    Accepts the tappable alias (``p-a1b2c3``), the displayed id, or a
    ``kind:id`` pair. Returns ``None`` when it names none or more than one —
    refusing is the only honest answer, because acting on either of two matches
    decides a proposal the owner never looked at.
    """
    from core.self_evolution import resolve_pending_alias

    items = list(getattr(pending, "items", pending) or [])
    want = str(target or "").strip()
    if not want:
        return None
    hit = resolve_pending_alias(want, items)
    if hit is not None:
        return hit
    from core.self_evolution import split_id_rev
    want, typed_rev = split_id_rev(want)  # 098 review: `<id>@<rev>` -> seat_rev
    exact = [it for it in items if str(it.get("id", "")) == want]
    if len(exact) == 1:
        return dict(exact[0], seat_rev=typed_rev) if typed_rev else exact[0]
    if typed_rev:
        return None
    qualified = [it for it in items
                 if f"{it.get('kind', '')}:{it.get('id', '')}" == want]
    return qualified[0] if len(qualified) == 1 else None


@dataclass(frozen=True)
class DecisionTarget:
    """The resolver's answer. ``item`` is what to decide (``None`` = decide nothing);
    ``ambiguous`` holds the ``kind:id`` names when the id matched more than one item."""

    item: Optional[Dict[str, Any]] = None
    ambiguous: Tuple[str, ...] = ()

    @property
    def refusal(self) -> str:
        """The owner-facing sentence for an ambiguous id ("" when not ambiguous)."""
        if not self.ambiguous:
            return ""
        return ("Two items share that id — use `<kind>:<id>`: "
                + ", ".join(self.ambiguous) + ".")


def resolve_decision_target(target: str, pending: Any) -> DecisionTarget:
    """The seam + the gone-tap fallback + the named ambiguity. Decide nothing on
    ``DecisionTarget.item is None``; show ``refusal`` when it is not empty."""
    match = resolve_pending_target(target, pending)
    if match is not None:
        return DecisionTarget(item=match)
    from core.self_evolution import split_id_rev
    want, _rev = split_id_rev(str(target or "").strip())
    items = list(getattr(pending, "items", pending) or [])
    shared = [it for it in items if str(it.get("id", "")) == want]
    if len(shared) > 1:
        return DecisionTarget(ambiguous=tuple(
            f"{it.get('kind', '')}:{it.get('id', '')}" for it in shared))
    if strip_tap_prefix(str(target or "").strip()) is not None:
        # A tool approval that left the open queue: only its lane can tell the
        # owner whether it was decided, expired or never existed.
        return DecisionTarget(item={"kind": "tool_approval",
                                    "id": str(target or "").strip()})
    return DecisionTarget()
