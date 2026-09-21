"""The Inbox endpoint (043 C5).

``GET  /api/webgate/inbox`` — the composed body every seat renders.
``POST /api/webgate/inbox/{kind}/{item_id}/{decide|reject|fulfill}`` — the
owner's decision, dispatched to the decider that already owns that kind.

Three rules this module exists to keep:

1. **It composes nothing.** :mod:`core.surfaces.inbox` does that, and the REPL's
   ``/inbox`` renders the same body from the same function. A second copy here
   is how two seats start disagreeing about what is waiting.
2. **It decides nothing.** Every verb routes to the function that already makes
   that decision on the CLI and on Telegram —
   ``webview.pages._decide_pending`` (self-evolution, tool approvals,
   correspondents), ``GoalBoard.decide_ask`` (asks),
   ``core.app_service.owner_ops`` (apps). A reimplemented decision is a second
   rule that drifts.
3. **A store that refuses is named, never omitted.** The collectors below are
   module-level so a test can replace one; each simply forwards to
   :mod:`surfaces.inbox_sources`, whose contract is to RAISE when a store
   cannot be read.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_INBOX_DECIDE
from core.surfaces.inbox import Item, compose  # noqa: F401 — the seat's own names
from core.surfaces.inbox import build_inbox as _compose_inbox
from webview import webgate
from webview.audit import console_write
from webview.copy import t

logger = logging.getLogger(__name__)

router = APIRouter()

#: How an operator fixes an unbound console. Deliberately NOT in the copy layer:
#: it names an environment variable, which the copy rules keep off a first
#: screen, and this rides only on the API refusal an operator reads in a
#: terminal.
UNBOUND_REMEDY = "Set POLYROB_OWNER_USER_ID."

#: Kinds whose decision is an ask transition on the goal board.
_ASK_KINDS = ("ask",)
#: Kinds whose decision is an app-service transition.
_APP_KINDS = ("app",)


# --- the seams -------------------------------------------------------------- #
# One function per source, so a test replaces a store rather than mocking a
# database. Each forwards to surfaces.inbox_sources, which raises when its store
# cannot be read — that raise is what becomes ``unreadable(<why>)``.

def _data_dir() -> str:
    return webgate.data_dir()


def _instance_id() -> str:
    from core.instance import resolve_instance_id
    return resolve_instance_id()


def _collect_self_evolution(user_id: str):
    from surfaces.inbox_sources import collect_self_evolution
    return collect_self_evolution(user_id, data_dir=_data_dir(),
                                  instance_id=_instance_id())


def _collect_tool_approvals(user_id: str):
    from surfaces.inbox_sources import collect_tool_approvals
    return collect_tool_approvals(user_id, data_dir=_data_dir())


def _collect_correspondents(user_id: str):
    from surfaces.inbox_sources import collect_correspondents
    return collect_correspondents(user_id, data_dir=_data_dir())


def _collect_asks(user_id: str):
    from surfaces.inbox_sources import collect_asks
    return collect_asks(user_id, data_dir=_data_dir())


def _collect_apps(user_id: str):
    from surfaces.inbox_sources import collect_apps
    return collect_apps(user_id, data_dir=_data_dir())


def _collectors() -> dict:
    """Resolved at CALL time, not import time, so a monkeypatched seam is the
    one that runs."""
    return {
        "self_evolution": _collect_self_evolution,
        "tool_approvals": _collect_tool_approvals,
        "correspondents": _collect_correspondents,
        "asks": _collect_asks,
        "apps": _collect_apps,
    }


#: How the composer spells a source that refused: ``unreadable(<why>)``.
_UNREADABLE = "unreadable("


def source_reasons(body: dict) -> dict:
    """``{source: reason}`` for every source that REFUSED — the typed field.

    ⚠️ 043 A36: the reason is the actionable half of "I could not read the
    spend approvals", and every seat that wanted it was slicing the composer's
    own prose (``sources[name][len("unreadable("):-1]``) at its render site. A
    string index in a template-facing function is a parser nobody declared. The
    parse now happens ONCE, here, next to the format it parses, and every seat
    reads a mapping.

    A source that answered is absent from the mapping (not present with an
    empty reason) — "it read fine" and "it refused for a reason I could not
    extract" are different facts, and the second keeps its entry with an empty
    string.
    """
    out = {}
    for name, state in (body.get("sources") or {}).items():
        text = str(state or "")
        if not text.startswith(_UNREADABLE):
            continue
        out[name] = text[len(_UNREADABLE):-1] if text.endswith(")") else ""
    return out


def with_source_reasons(body: dict) -> dict:
    """Stamp :func:`source_reasons` onto a composed Inbox *body*, in place."""
    body["source_reasons"] = source_reasons(body)
    return body


def build_inbox(user_id: str) -> dict:
    """The composed Inbox for *user_id* over this console's five stores."""
    return with_source_reasons(_compose_inbox(user_id, _collectors()))


# --- tenant ----------------------------------------------------------------- #

def unbound_console() -> Optional[str]:
    """The reason this console cannot name a tenant, or ``None`` if it can.

    ⚠️ ``webgate.local_owner_id()`` does NOT raise when no owner is bound — it
    warns once and falls back to the local tenant
    (``core.instance.resolve_owner_user_id``; it was the instance id ``polyrob``
    before 2026-09-15). So every read on an unbound console is silently scoped
    to the tenant ``local`` and, on a headless server whose agent is bound to a
    different owner, renders an honest-LOOKING empty list for an agent with
    plenty waiting under the real owner's id. On the Inbox that reads as
    **"Nothing needs you."** over the owner's real queue — the confident zero
    this whole surface exists to refuse.

    ⚠️ It fires on ``own_ops`` ONLY, because that is the only posture where the
    owner binding is what names the tenant. ``local`` has exactly one owner by
    construction, so the unbound default IS the owner. And ``multitenant``
    never consults ``local_owner_id`` at all — ``pages._effective_user_id``
    takes the tenant from the AUTHENTICATED caller and 403s without one, so
    refusing there would lock every authenticated tenant out of their own
    inbox over a binding their console does not use.
    """
    try:
        if webgate.posture() != "own_ops":
            return None
        if webgate.owner_is_bound():
            return None
        return t("inbox.unbound_owner")
    except Exception:
        logger.warning("owner-binding probe failed", exc_info=True)
        return t("inbox.owner_unreadable")


def _tenant(request: Request) -> str:
    """Fail-CLOSED tenant identity — the same resolution every console read uses.

    Two refusals, and neither may become a zero: ``_effective_user_id`` 403s a
    multitenant caller with no identity, and :func:`unbound_console` names a
    console whose owner was never chosen — which otherwise resolves to the
    instance id and scopes every read to a tenant nobody uses.

    ⚠️ Since 043 residue W1 the unbound check below is a DUPLICATE by design:
    ``pages._effective_user_id`` now raises the identical exception, so deleting
    it here would change nothing. Do not "consolidate" the other two, which are
    not duplicates. ``pages_new._tenant`` is the LOAD-BEARING one — it RETURNS
    the refusal instead of raising, because a 500 on the shell takes every
    screen down with it. And ``pages._effective_user_id`` is the one that
    reaches the 34 legacy readers, which never call this function at all.
    """
    unbound = unbound_console()
    if unbound:
        # The REMEDY names the variable; the reason does not. An operator reads
        # an API refusal in a terminal, where the variable name is the useful
        # half — but the same sentence renders on the Inbox page, and a flag
        # name on a first screen is what the copy rules forbid.
        raise HTTPException(status_code=403, detail=f"{unbound} {UNBOUND_REMEDY}")
    from webview.pages import _effective_user_id
    return _effective_user_id(request)


# --- deciders (reused, never reimplemented) --------------------------------- #

def _decide_pending(kind: str, item_id: str, kw: dict, *, approved: bool):
    from webview.pages import _decide_pending as shared
    return shared(kind, item_id, kw, approved=approved)


def _goal_board():
    from webview.pages import _webgate_goal_board
    return _webgate_goal_board()


def _decide_app(slug: str, user_id: str, approved: bool):
    from core.app_service import owner_ops
    from core.app_service.registry import AppServiceRegistry, default_app_services_db
    fn = owner_ops.approve if approved else owner_ops.reject
    return fn(AppServiceRegistry(default_app_services_db()), slug, user_id,
              via="webview")


def _unblocked_text(unblocked: int) -> str:
    """What an ask's decision actually freed. "1 goals" is the tell that a
    count was pasted into a sentence rather than written into one."""
    if not unblocked:
        return t("inbox.ask.decided_none")
    if unblocked == 1:
        return t("inbox.ask.decided_one")
    return t("inbox.ask.decided", count=unblocked)


#: How much of an owner's answer is kept on the ask. Long enough for a real
#: answer ("use the staging key, it is in 1Password under X"), bounded because
#: it is written into a JSON payload the dispatcher reads on every claim.
ANSWER_MAX_CHARS = 2000


# ⚠️ There is exactly ONE writer of an ask's answer, and it is
# ``GoalBoard.decide_ask(answer=...)`` — it stamps ``payload.answer`` on the ask
# and ``payload.owner_unblocked.answer`` on each dependent goal inside the same
# tenant-scoped CAS as the decision. A console-side ``merge_payload`` fallback
# lived here until 2026-09-21 with no caller left; a second writer for one field
# is how the ask and the goal come to disagree about what the owner said.


def _decide(request: Request, kind: str, item_id: str, *, approved: bool,
            answer: str = "") -> dict:
    user_id = _tenant(request)
    if kind in _APP_KINDS:
        from webview.pages import _owner_console_required
        _owner_console_required(t("inbox.owner_console"))
        ok, msg = _decide_app(item_id, user_id, approved)
    elif kind in _ASK_KINDS:
        board = _goal_board()
        # A27: the owner's answer rides INTO the decision (board.decide_ask
        # stamps payload.answer on the ask and owner_unblocked.answer on each
        # dependent goal, so the retry prompt renders what the owner said).
        ok, unblocked = board.decide_ask(
            item_id, user_id=user_id, approved=approved,
            answer=" ".join(str(answer or "").split())[:ANSWER_MAX_CHARS])
        msg = t("inbox.ask.gone") if not ok else _unblocked_text(unblocked)
    else:
        kw = {"user_id": user_id, "home_dir": _data_dir(),
              "instance_id": _instance_id()}
        ok, msg = _decide_pending(kind, item_id, kw, approved=approved)
    # W4: name the actor + `via=webview`. The app-kind path ALSO records its
    # own domain event downstream (owner_ops APP_APPROVED/REJECTED); this is the
    # distinct "who used the console inbox" audit row, not a duplicate of it.
    console_write(CONSOLE_INBOX_DECIDE, user_id=user_id,
                  attrs={"kind": kind, "item_id": item_id,
                         "approved": approved, "ok": bool(ok)})
    return {"ok": bool(ok), "message": str(msg)}


# --- routes ----------------------------------------------------------------- #

@router.get("/api/webgate/inbox")
async def api_inbox(request: Request):
    """What is waiting on a person, and which lists could not be read.

    Read-only and tenant-scoped. A refusing store is a named source and a
    visible entry — never a 500, and never a silently smaller list.
    """
    user_id = _tenant(request)
    body = build_inbox(user_id)
    body["user_id"] = user_id
    return JSONResponse(body)


async def _answer_text(request: Request) -> str:
    """The optional ``{"answer": "<text>"}`` an ask's decision may carry.

    Every decide/reject/fulfill accepts it and only an ASK records it; a body
    that is absent, empty or not an object is simply no answer, never a 400 —
    the decision is the thing being made, and refusing it over a malformed
    optional field would lose the decision too.
    """
    try:
        body = await request.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("answer") or "")


@router.post("/api/webgate/inbox/{kind}/{item_id}/decide",
             dependencies=webgate.MUTATION_DEPS)
async def api_inbox_decide(request: Request, kind: str, item_id: str):
    return JSONResponse(_decide(request, kind, item_id, approved=True,
                                answer=await _answer_text(request)))


@router.post("/api/webgate/inbox/{kind}/{item_id}/reject",
             dependencies=webgate.MUTATION_DEPS)
async def api_inbox_reject(request: Request, kind: str, item_id: str):
    return JSONResponse(_decide(request, kind, item_id, approved=False,
                                answer=await _answer_text(request)))


@router.post("/api/webgate/inbox/{kind}/{item_id}/fulfill",
             dependencies=webgate.MUTATION_DEPS)
async def api_inbox_fulfill(request: Request, kind: str, item_id: str):
    """``fulfill`` is an ask's own word for approve (it unblocks the goals that
    stalled on it). Refused for any other kind rather than quietly aliased —
    a verb that silently means something else on half its inputs is worse than
    a 400."""
    if kind not in _ASK_KINDS:
        raise HTTPException(status_code=400, detail=t("inbox.fulfill.refused"))
    return JSONResponse(_decide(request, kind, item_id, approved=True,
                                answer=await _answer_text(request)))
