"""A room's LABEL is not a room's NAME (057 WS-D / R3).

``group_allowlist.note`` is what the owner typed after
``polyrob owner groups allow <surface> <chat_id>`` — the CLI calls it "Label".
Three renderers promoted it to the room's quoted TITLE, so a note written once
as a reminder ("the den, ask before posting") came back months later looking
like the room's actual name, and the agent reasoned about it as if Telegram had
told it so. The 09-19 "fix" was a data edit: somebody retyped the note. The code
still lied.

This module is the ONE rendering rule:

- the room's **name** comes from ``chat_policy.chat.name`` and nowhere else —
  it is the only field an owner sets as a NAME. With none set the room renders
  as ``<surface>:<chat_id>``, which is always true.
- the owner's **note** renders as an explicitly dated LABEL:
  ``label: "<note>" (set 2026-09-19)`` — never in the title position, and
  never without the date that makes it checkable.

Pure core: stdlib only at module top; ``chat_policy`` is imported lazily by the
name resolver so this module stays importable from the tools tier.
"""
from __future__ import annotations

import time
from typing import Any, Mapping, Optional


def _stamp(ts) -> str:
    """``YYYY-MM-DD`` from an epoch seconds value, or ``""`` when unknown.

    An unreadable timestamp renders NO date rather than today's — a label dated
    by accident is the same defect one layer down.
    """
    try:
        if ts in (None, "", 0):
            return ""
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
    except Exception:
        return ""


def render_room_label(row: Mapping[str, Any]) -> str:
    """The owner's note on an allowlist ``row``, rendered as a dated LABEL.

    Returns ``""`` when the room has no note (so a caller can join it away).
    Uses ``updated_at`` when the row carries one — a re-``allow`` rewrites the
    note, and dating it by the ORIGINAL ``created_at`` would over-age it — else
    ``created_at``.
    """
    note = str((row or {}).get("note") or "").strip()
    if not note:
        return ""
    when = _stamp((row or {}).get("updated_at")) or _stamp((row or {}).get("created_at"))
    return f'label: "{note}" (set {when})' if when else f'label: "{note}"'


def room_name(row: Mapping[str, Any], policy: Any = None) -> str:
    """The room's NAME: ``chat.name`` if the owner set one, else
    ``<surface>:<chat_id>``. NEVER the allowlist note.

    ``policy`` may be a loaded ``ChatPolicy`` (the caller already has one on the
    status/admin paths); omitted, the name is simply absent and the fallback is
    used — this function never reads the filesystem on its own.
    """
    surface = str((row or {}).get("surface") or "")
    chat_id = str((row or {}).get("chat_id") or "")
    name = str(getattr(policy, "name", "") or "").strip()
    return name or f"{surface}:{chat_id}"


def render_room_line(row: Mapping[str, Any], policy: Any = None,
                     extra: Optional[str] = None) -> str:
    """``<surface>:<chat_id> "<name>"[ — label: "<note>" (set <date>)][ — extra]``.

    The ONE room line every owner/agent seat renders. The label follows the name
    and is visibly a label; there is no position in this string where a note can
    be mistaken for what the room is called.
    """
    surface = str((row or {}).get("surface") or "")
    chat_id = str((row or {}).get("chat_id") or "")
    parts = [f'{surface}:{chat_id} "{room_name(row, policy)}"']
    label = render_room_label(row)
    if label:
        parts.append(label)
    if extra:
        parts.append(str(extra))
    return " — ".join(parts)


__all__ = ["render_room_label", "render_room_line", "room_name"]
