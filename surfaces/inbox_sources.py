"""The Inbox's concrete readers (043 C5).

:mod:`core.surfaces.inbox` is pure and takes its collectors as an argument.
This module is the other half: the five real stores, each wrapped so that

* a store it can read contributes :class:`~core.surfaces.inbox.Item` rows;
* a store it **cannot** read **raises**, which the composer turns into a named
  ``unreadable(<why>)`` source and a visible entry. Returning ``[]`` from here
  would be a lie with the same shape as the truth.

It lives in ``surfaces/`` rather than ``core/`` because it reaches the goal
board (``agents.*``), the approval queue (``tools.*``) and the app registry —
every one of them a downward import from tier 4, which the layering ratchet
allows and which ``core/`` may not do.

⚠️ **A read never creates a store.** Every one of these stores initialises its
schema in its constructor, so constructing one to answer "is anything waiting"
would leave an empty database in whatever data home happened to resolve — the
exact status-SSOT rule ``core.wallet.bridge_guard.open_bridges`` states in
prose. A missing file is a real answer ("nothing was ever recorded"), so it is
reported as ``ok`` with no rows; a file that exists and refuses to open raises.

⚠️ **Tenant scoping is this module's job.**
``AppServiceRegistry.list_by_status`` is instance-wide by design (the
supervisor needs every tenant's rows), so the app collector filters by
``user_id`` here. An Inbox that shows another tenant's pending app is a leak,
not a listing.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from core.surfaces.inbox import Item

logger = logging.getLogger(__name__)

#: How much of a store's own preview text a card carries. Long enough to be a
#: decision, short enough not to become the page.
_TITLE_CHARS = 110
_BODY_CHARS = 600


def _data_dir(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    from core.runtime_paths import resolve_data_home
    return resolve_data_home()


def _instance_id(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    from core.instance import resolve_instance_id
    return resolve_instance_id()


def _first_line(text: str) -> str:
    """The first readable line of a store's preview, as a card title."""
    line = " ".join(str(text or "").strip().split())
    if len(line) <= _TITLE_CHARS:
        return line
    return line[:_TITLE_CHARS - 1].rstrip() + "…"


def _body(text: str) -> str:
    body = str(text or "").strip()
    return body if len(body) <= _BODY_CHARS else body[:_BODY_CHARS - 1] + "…"


def _store(path: str) -> Optional[str]:
    """*path* if the store exists, else ``None`` — never create it on a read."""
    return path if os.path.isfile(path) else None


# --------------------------------------------------------------------------- #
# the five collectors
# --------------------------------------------------------------------------- #

def collect_self_evolution(user_id: str, *, data_dir: str,
                           instance_id: str) -> List[Item]:
    """Quarantined proposals Rob wrote and will not apply until a person looks.

    ⚠️ ``core.self_evolution.list_pending`` swallows each pipeline's own read
    failure internally (it logs and contributes nothing), so a partially broken
    identity tier reads here as "fewer proposals" rather than as unreadable.
    That is the aggregator's existing contract and is not re-litigated here; a
    failure of the aggregator ITSELF still raises and is named.
    """
    from core import self_evolution
    out: List[Item] = []
    for row in self_evolution.list_pending(user_id, home_dir=data_dir,
                                           instance_id=instance_id) or []:
        kind = str(row.get("kind") or "")
        preview = row.get("preview") or ""
        conflicts = row.get("conflicts") or []
        out.append(Item(
            kind=kind,
            id=str(row.get("id") or ""),
            title=_first_line(preview) or self_evolution.pending_kind_label(kind),
            body=_body(preview),
            meta=("; ".join(str(c) for c in conflicts))[:300],
            blocking=True,
            actions=("approve", "reject", "show"),
            source="self_evolution",
            extra={"label": self_evolution.pending_kind_label(kind),
                   "path": row.get("path") or ""},
        ))
    return out


def collect_tool_approvals(user_id: str, *, data_dir: str) -> List[Item]:
    """Spend and tool grants the agent stopped on and queued for the owner."""
    from tools.controller.approval_queue import list_pending_tool_approvals
    path = _store(os.path.join(data_dir, "goals.db"))
    if path is None:
        return []
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(path)
    out: List[Item] = []
    for row in list_pending_tool_approvals(board, user_id) or []:
        preview = row.get("preview") or ""
        out.append(Item(
            kind=str(row.get("kind") or "tool_approval"),
            id=str(row.get("id") or ""),
            title=_first_line(preview),
            body=_body(preview),
            blocking=True,
            actions=("approve", "reject"),
            source="tool_approvals",
        ))
    return out


def collect_correspondents(user_id: str, *, data_dir: str) -> List[Item]:
    """Third parties whose replies stay unroutable until the owner decides.

    The registry is PROBED first, deliberately.
    ``core.surfaces.owner_admin.pending_correspondent_items`` catches its own
    read failure and returns what it collected, which would present an
    unreadable registry as an empty one. The probe is the read that is allowed
    to raise; the listing itself is still that shared builder, not a second
    copy of the filter.
    """
    from core.surfaces.correspondents import CorrespondentRegistry
    from core.surfaces.owner_admin import pending_correspondent_items
    path = _store(os.path.join(data_dir, "correspondents.db"))
    if path is None:
        return []
    registry = CorrespondentRegistry(path)
    registry.list(user_id=user_id)  # the probe — an unreadable store raises here
    out: List[Item] = []
    for row in pending_correspondent_items(registry, user_id) or []:
        preview = str(row.get("preview") or "")
        # The shared preview ends with the CLI/chat remedy, which a seat with
        # buttons renders as buttons. Keep the fact, drop the instruction.
        fact = preview.split("  (approve:")[0].strip()
        # ⚠️ The TITLE is the sentence, not the id. `telegram:12345` is routing
        # grammar: it says nothing about what is being decided, and it is the
        # line a person reads first on a card whose whole job is to be decided.
        # The address stays in the body, where it identifies who.
        item_id = str(row.get("id") or "")
        surface = item_id.split(":", 1)[0] if ":" in item_id else ""
        title = (f"{surface} contact awaiting approval" if surface
                 else "A contact awaiting approval")
        out.append(Item(
            kind="correspondent",
            id=item_id,
            title=title,
            body=_body(fact),
            blocking=True,
            actions=("approve", "reject"),
            source="correspondents",
        ))
    return out


def collect_asks(user_id: str, *, data_dir: str) -> List[Item]:
    """Open asks — work that stalled on something only the owner can give.

    Tool-approval asks live on the same table and are EXCLUDED here: they have
    their own collector, their own id shape (``tap-``) and their own decider,
    so listing them twice under two ids is how an owner decides one thing and
    still sees it waiting.
    """
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND
    path = _store(os.path.join(data_dir, "goals.db"))
    if path is None:
        return []
    board = GoalBoard(path)
    out: List[Item] = []
    for ask in board.asks(user_id=user_id, status=ASK_OPEN) or []:
        payload = ask.payload or {}
        if payload.get("ask_kind") == TOOL_APPROVAL_ASK_KIND:
            continue
        blocked = payload.get("blocks_goal_ids") or []
        out.append(Item(
            kind="ask",
            id=str(ask.id),
            title=_first_line(ask.title),
            body=_body(ask.body or ""),
            blocking=True,
            created_at=float(ask.created_at or 0.0),
            actions=("fulfill", "reject"),
            source="asks",
            extra={"blocks": len(blocked)},
        ))
    return out


def collect_apps(user_id: str, *, data_dir: str) -> List[Item]:
    """Apps Rob built and stopped on, waiting for an address to go live at.

    ``list_by_status`` is instance-wide; the tenant filter is applied here.
    """
    from core.app_service.registry import AppServiceRegistry, default_app_services_db
    del data_dir  # the app registry resolves its own path (APP_SERVICES_DB_PATH)
    path = _store(default_app_services_db())
    if path is None:
        return []
    registry = AppServiceRegistry(path)
    out: List[Item] = []
    for row in registry.list_by_status(["pending"]) or []:
        if str(row.get("user_id") or "") != str(user_id):
            continue
        slug = str(row.get("slug") or "")
        cmd = " ".join(str(c) for c in (row.get("cmd") or []))
        out.append(Item(
            kind="app",
            id=slug,
            title=slug,
            body=_body(cmd),
            meta=str(row.get("egress") or ""),
            blocking=True,
            created_at=float(row.get("created_at") or 0.0),
            actions=("approve", "reject", "show"),
            source="apps",
            extra={"port": row.get("container_port"),
                   "health_path": row.get("health_path") or "",
                   "egress": row.get("egress") or ""},
        ))
    return out


# --------------------------------------------------------------------------- #
# the wiring
# --------------------------------------------------------------------------- #

def default_collectors(*, data_dir: Optional[str] = None,
                       instance_id: Optional[str] = None
                       ) -> Dict[str, Callable[[str], Any]]:
    """The five collectors, keyed by the names
    :data:`core.surfaces.inbox.SOURCE_LABELS` labels, in read order.

    Each is bound to one data home so a caller with its own resolved root (the
    console, a test, a profile-scoped REPL) never falls back to the process
    default.
    """
    home = _data_dir(data_dir)
    inst = _instance_id(instance_id)
    return {
        "self_evolution": lambda uid: collect_self_evolution(
            uid, data_dir=home, instance_id=inst),
        "tool_approvals": lambda uid: collect_tool_approvals(uid, data_dir=home),
        "correspondents": lambda uid: collect_correspondents(uid, data_dir=home),
        "asks": lambda uid: collect_asks(uid, data_dir=home),
        "apps": lambda uid: collect_apps(uid, data_dir=home),
    }


def build_inbox(user_id: str, *, data_dir: Optional[str] = None,
                instance_id: Optional[str] = None) -> Dict[str, Any]:
    """The composed Inbox for *user_id* over the five real stores."""
    from core.surfaces.inbox import build_inbox as compose_inbox
    return compose_inbox(user_id, default_collectors(data_dir=data_dir,
                                                     instance_id=instance_id))
