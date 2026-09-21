"""043 phase 1 — the five destinations, behind one shell.

The console previously presented fifteen flat top-level destinations. The
current information architecture presents **five**, and five is not a matter of
taste: it is the ceiling for a bottom bar, and the bottom bar is what the nav
becomes below 768 px. So the number is a mobile-viability constraint.

This module owns the frame and nothing else yet. Each destination renders a
placeholder that says plainly what is not built and where the capability lives
meanwhile — never a blank page, never a spinner that never resolves. The
screens themselves arrive in the following batches and replace the placeholders
one at a time; the frame does not move when they do.

Why a separate module from ``webview/pages.py``: that file is the v1 webgate,
1500 lines of read endpoints and the one surviving page (/pending). The shell is
independently mountable — ``mount(app)`` never shadows a route the app already
serves, so it registers the five destinations beside those endpoints. As of 043
phase 5 this is the ONLY console; the ``WEBVIEW_UI`` switch and the legacy pages
are gone.

**Everything a person reads comes from** :mod:`webview.copy`. A string literal
in this module or in ``shell.html`` is a bug that
``tests/unit/webview/test_copy_layer_ratchet.py`` fails on.
"""
import logging
import os
import time

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from core.event_kinds import (
    CONSOLE_CRON_CREATE,
    CONSOLE_GOAL_CREATE,
    CONSOLE_MEMORY_WRITE,
    CONSOLE_SELF_CONTEXT_WRITE,
)
from webview import webgate
from webview.audit import console_write
from webview.copy import t

logger = logging.getLogger(__name__)

router = APIRouter()

#: JSON readers/writers ride here, NOT on ``router`` (see ``_API_ROUTERS``): they
#: are readers the console's pages depend on, mounted unconditionally. (They were
#: once wrongly gated behind the WEBVIEW_UI page switch, which hid the Inbox API.)
api_router = APIRouter()


# --- the five destinations, one table --------------------------------------- #
# Glyphs are decorative and aria-hidden; the label is the accessible name and it
# comes from the copy layer. Order is the nav order at both sizes.
NAV = (
    {"key": "new", "href": "/", "glyph": "✎", "label": "nav.new"},
    {"key": "inbox", "href": "/inbox", "glyph": "▣", "label": "nav.inbox"},
    {"key": "work", "href": "/work", "glyph": "◷", "label": "nav.work"},
    {"key": "money", "href": "/money", "glyph": "◈", "label": "nav.money"},
    {"key": "agent", "href": "/agent", "glyph": "⚙", "label": "nav.agent"},
)

_GLYPH = {item["key"]: item["glyph"] for item in NAV}


# --- templates -------------------------------------------------------------- #

def _templates() -> Jinja2Templates:
    """Own environment, same asset base as ``server.py`` and ``pages.py``.

    Separate from ``pages._TEMPLATES`` on purpose: the copy layer is registered
    as a Jinja global here and nowhere else, so a legacy template cannot start
    depending on it by accident.
    """
    try:
        from core.assets import webgate_asset_dir
        templates_dir = webgate_asset_dir() / "templates"
    except Exception:  # fail-open to the repo checkout
        templates_dir = Path(__file__).resolve().parent / "templates"
    env = Jinja2Templates(directory=str(templates_dir))
    from webview.theme import theme_preference, show_avatar
    env.env.globals["theme_preference"] = theme_preference
    env.env.globals["show_avatar"] = show_avatar
    env.env.globals["t"] = t
    # The ONE upload allowlist (A24): the bound-session composer's picker
    # renders `accept=` from the same list the /workspace/upload endpoint
    # enforces. Without this global the destinations' env rendered no accept.
    from core.surfaces.inbound_attachments import upload_accept_attribute
    env.env.globals["upload_accept"] = upload_accept_attribute()
    return env


_TEMPLATES = _templates()


# --- the seams the shell reads ---------------------------------------------- #
# Each is a module-level function so a test can replace it, and so the
# write-posture batch can move the real readers behind them without touching
# the template. They are the only place this module talks to the rest of POLYROB.

def _read_only() -> bool:
    """``WEBVIEW_READ_ONLY`` — stated once in the frame, not one button at a time."""
    try:
        return bool(webgate.read_only())
    except Exception:
        logger.debug("read-only probe failed", exc_info=True)
        return False


def _owner_console() -> bool:
    """Is this seat the OWNER's console? Every instance-wide control asks first.

    ⚠️ Three controls refuse a multitenant caller inside their handler —
    pause/resume (the 031 record is instance-wide), the app decisions
    (`apps_routes._decide`) and the Inbox's app-kind approve/reject
    (`inbox._decide`) — and all three were DRAWN on ``read_only`` alone. An
    authenticated tenant therefore saw a Pause button on all five destinations,
    a Stop on every live app and Approve/Reject on every app card, each of
    which answers 403 on the click. One predicate,
    ``webgate.is_owner_console()``, now decides both the refusal and the draw;
    it rides into the client modules on their copy nodes as
    ``data-owner_console``.

    Fail-CLOSED: a probe that cannot run draws no instance-wide control.
    """
    try:
        return bool(webgate.is_owner_console())
    except Exception:
        logger.debug("owner-console probe failed", exc_info=True)
        return False


def _when(ts) -> str:
    """A moment a person can act on: the clock, plus the day when it is not today.

    ``core.status_render`` prints a bare ``%H:%M UTC``, which is right in a
    terminal beside a date. On the one line every console screen carries, a bare
    clock reads as "in a few minutes" when it is really tomorrow morning — so
    the day is part of the answer whenever it is not today.
    """
    try:
        when = time.gmtime(float(ts))
    except (TypeError, ValueError, OverflowError):
        return "?"
    clock = time.strftime("%H:%M UTC", when)
    today = time.gmtime()
    days = (time.mktime(when[:3] + (0, 0, 0) + when[6:])
            - time.mktime(today[:3] + (0, 0, 0) + today[6:])) / 86400.0
    if -0.5 < days < 0.5:
        return clock
    if 0.5 < days < 1.5:
        return t("shell.when.tomorrow", time=clock)
    return t("shell.when.dated", time=clock,
             date=time.strftime("%d %b", when).lstrip("0"))


def _scope_words(scopes) -> str:
    """The same words the terminal prints (``owner_admin._scope_words``),
    reused rather than re-worded — two seats naming one pause differently is
    how an owner stops trusting either."""
    try:
        from core.surfaces.owner_admin import _scope_words as shared
        return shared(tuple(scopes or ()))
    except Exception:
        logger.debug("scope words fell back", exc_info=True)
        return ", ".join(str(s) for s in (scopes or ()))


def _pause_headline() -> str:
    """The first half of the head truth, from the ONE pause record (031).

    ``core.autonomy_control.read_state`` is fail-CLOSED: an unreadable record IS
    a pause of everything, because that is what the runtime then does. So an
    unreadable record renders as paused **and says why** — reporting it as
    "running" would be the confident-wrong answer the whole product is built to
    avoid, and reporting it as "unknown" would be false in the other direction.
    Only a probe that cannot run at all is genuinely unknown.

    ⚠️ A pause has SCOPES. ``/pause trading`` leaves everything else running,
    and every other seat says so; a console that renders it as a bare "Rob is
    paused." is confidently wrong on the one line that may never be. So the
    scope words are rendered whenever the pause is not ``all``.
    """
    try:
        from core.surfaces.owner_admin import pause_state
        state = pause_state(webgate.data_dir())
    except Exception:
        logger.debug("pause record probe failed", exc_info=True)
        return t("shell.state.unknown")
    if not getattr(state, "paused", False):
        return t("shell.state.running")
    if getattr(state, "source", "") == "unreadable":
        return t("shell.state.paused_unreadable")
    until = getattr(state, "until", None)
    scopes = tuple(getattr(state, "scopes", ()) or ())
    everything = (not scopes) or ("all" in scopes)
    if everything:
        if until:
            return t("shell.state.paused_until", until=_when(until))
        return t("shell.state.paused")
    what = _scope_words(scopes)
    if until:
        return t("shell.state.paused_scoped_until", what=what, when=_when(until))
    return t("shell.state.paused_scoped", what=what)


def _tenant(request: Request) -> tuple:
    """``(user_id, refusal)`` — the tenant every destination scopes to.

    Fail-CLOSED, and it does NOT raise: a 500 on the shell would take every
    screen down with it, so the refusal is RETURNED and the caller renders it as
    UNKNOWN — never as an empty list, which is the confident-wrong answer this
    frame exists to avoid.

    Two refusals reach here. ``webview.pages._effective_user_id`` 403s a
    multitenant caller with no identity. And ⚠️ an UNBOUND console
    (``webview.inbox.unbound_console``) — which does not raise anywhere:
    ``local_owner_id`` warns once and answers the local tenant, so without this
    check the badge and the Inbox would report a confident zero over the real
    owner's queue.
    """
    try:
        from webview.inbox import unbound_console
        unbound = unbound_console()
        if unbound:
            return (None, unbound)
        from webview.pages import _effective_user_id
        return (_effective_user_id(request), None)
    except Exception as exc:
        logger.debug("tenant resolution refused", exc_info=True)
        from core.surfaces.inbox import why
        return (None, why(exc))


def _compose_inbox(user_id: str) -> dict:
    """The five-source Inbox for *user_id*. Replaced wholesale in tests."""
    from webview.inbox import build_inbox
    return build_inbox(user_id)


def _no_tenant_summary(refusal: str) -> dict:
    """An Inbox with no tenant to read for: every source UNKNOWN, nothing zero."""
    from core.surfaces.inbox import SOURCE_LABELS, compose, unreadable_item
    from webview.inbox import with_source_reasons
    return with_source_reasons(
        compose([unreadable_item(name) for name in SOURCE_LABELS],
                {name: f"unreadable({refusal})" for name in SOURCE_LABELS}))


def _inbox_summary(request: Request) -> dict:
    """The composed Inbox body, read ONCE per request.

    The frame's badge and the Inbox page render the same dict: five stores read
    once, not once per nav item and not twice per page. Cached on
    ``request.state`` because every destination carries the badge.
    """
    cached = getattr(request.state, "polyrob_inbox", None)
    if cached is not None:
        return cached
    user_id, refusal = _tenant(request)
    if not user_id:
        body = _no_tenant_summary(refusal or "")
    else:
        try:
            body = _compose_inbox(user_id)
        except Exception as exc:
            logger.warning("inbox composition failed", exc_info=True)
            from core.surfaces.inbox import why
            body = _no_tenant_summary(why(exc))
    try:
        request.state.polyrob_inbox = body
    except Exception:
        logger.debug("request state is not writable", exc_info=True)
    return body


def _inbox_state(request: Request) -> tuple:
    """``(count, partial, lists)`` — how many DECISIONS wait, whether a source
    refused, and HOW MANY refused.

    The badge counts decisions (owner, 2026-09-13): an informational card is
    listed on the page and not counted. ``partial`` is the honest-state rule in
    pixels — when one of the Inbox's sources could not be read, the count is a
    FLOOR, the badge is drawn dashed and reads ``1+``, and the label says a list
    is unreadable. A zero over an unread source is the one thing it may not be.
    """
    body = _inbox_summary(request)
    lists = len(body.get("unreadable_sources") or ())
    uncertain = bool(body.get("uncertain"))
    # An unreadable ITEM with no source marker still makes the count a floor,
    # and "one list" is the honest floor for it too.
    return (int(body.get("count") or 0), uncertain,
            max(1, lists) if uncertain else 0)


# --- the shell context ------------------------------------------------------ #

def _badge(count: int, partial: bool, lists: int = 1) -> tuple:
    """``(badge_text, aria_label)`` for the Inbox link.

    ⚠️ The label counts the unreadable LISTS as well as the waiting items:
    saying "one list" over three of them is a small lie in the direction of "it
    is fine", which is the one direction this badge may never lean.
    """
    if partial:
        text = f"{count}+"
        if max(1, lists) > 1:
            label = (t("nav.inbox_uncertain_one_lists", lists=lists) if count == 1
                     else t("nav.inbox_uncertain_lists", count=count, lists=lists))
        else:
            label = (t("nav.inbox_uncertain_one") if count == 1
                     else t("nav.inbox_uncertain", count=count))
        return text, label
    if count:
        label = (t("nav.inbox_waiting_one") if count == 1
                 else t("nav.inbox_waiting", count=count))
        return str(count), label
    return "", ""


def _waiting_line(count: int, partial: bool, lists: int = 1) -> str:
    if partial:
        if max(1, lists) > 1:
            if count == 0:
                return t("shell.waiting.uncertain_none_lists", lists=lists)
            if count == 1:
                return t("shell.waiting.uncertain_one_lists", lists=lists)
            return t("shell.waiting.uncertain_many_lists", count=count, lists=lists)
        if count == 0:
            return t("shell.waiting.uncertain_none")
        if count == 1:
            return t("shell.waiting.uncertain_one")
        return t("shell.waiting.uncertain_many", count=count)
    if count == 0:
        return t("shell.waiting.none")
    if count == 1:
        return t("shell.waiting.one")
    return t("shell.waiting.many", count=count)


def _wordmark() -> str:
    """The instance's own name, in the display face. Not the product's."""
    try:
        from core.instance import resolve_instance_id
        return str(resolve_instance_id())
    except Exception:
        logger.debug("instance id probe failed", exc_info=True)
        return "polyrob"


def _live_count(request: Request) -> tuple:
    """``(count, partial)`` — things IN PROGRESS for the shell's tenant.

    ``count`` is running goals + running cron jobs + live sessions
    (``_live_body``). ``partial`` is 043 A37: when a whole store could not be
    read the number is a FLOOR, and the head line must say ``N+`` rather than
    ``N``. ``(0, True)`` when the tenant cannot be named or the probe itself
    fails — the head line then says only "running", which is true, and never
    claims a count it did not measure.
    """
    try:
        user_id, _refusal = _tenant(request)
        if not user_id:
            return (0, True)
        body = _live_body(str(user_id), webgate.data_dir(), _sessions_root())
        raw = body.get("count")
        return (int(raw or 0), bool(body.get("partial")) or raw is None)
    except Exception:
        logger.debug("live count probe failed", exc_info=True)
        return (0, True)


def _headline(request: Request) -> str:
    """The pause half of the head truth, with the in-progress count folded in
    when Rob is running. "Rob is running." over three mid-flight cron runs read
    as idle (2026-09-16 owner report); the count is what makes it true."""
    headline = _pause_headline()
    if headline != t("shell.state.running"):
        return headline
    busy, partial = _live_count(request)
    if busy > 0:
        # ``N+`` when a store refused: the count is a floor, and a bare number
        # over an unread store is the confident figure this frame refuses.
        return t("shell.state.running_busy",
                 count=(f"{busy}+" if partial else busy))
    return headline


def _shell_context(request: Request, current: str) -> dict:
    count, partial, lists = _inbox_state(request)
    badge, aria = _badge(count, partial, lists)
    return {
        "request": request,
        "nav_items": NAV,
        "nav_current": current,
        # The <title> is the frame's own sentence, not the destination name
        # pasted into a tab. One key, one shape, every screen.
        "page_title": t("shell.title", page=t(f"{current}.title")),
        "wordmark": _wordmark(),
        "pause_headline": _headline(request),
        "waiting_line": _waiting_line(count, partial, lists),
        "inbox_badge": badge,
        "inbox_aria": aria,
        "inbox_partial": partial,
        "read_only": _read_only(),
        "owner_console": _owner_console(),
        "show_logout": _show_logout(),
    }


def _show_logout() -> bool:
    """A logout link exists only where a login does (own_ops / multitenant);
    the `local` operator IS the owner and has no session to end."""
    try:
        return bool(webgate.requires_owner_login())
    except Exception:
        return False


# --- the Inbox ------------------------------------------------------------- #
# The one genuinely new screen. Everything it renders comes from the composed
# body (``core.surfaces.inbox``) — this builds only the words around it.

#: A card's own verb, where it has a better one than the shared vocabulary.
#: ``(kind, action) -> copy key``; anything unlisted falls back to
#: ``inbox.action.<action>``.
_ACTION_WORDS = {
    ("app", "approve"): "inbox.action.publish",
    ("app", "reject"): "inbox.action.not_now",
    ("skill", "approve"): "inbox.action.keep",
    ("skill", "reject"): "inbox.action.discard",
    ("skill", "show"): "inbox.action.read_it",
    ("app", "show"): "inbox.action.see_details",
    ("unreadable", "show"): "inbox.unreadable.why",
}

#: Which endpoint verb each action posts to.
_ACTION_VERB = {"approve": "decide", "reject": "reject", "fulfill": "fulfill"}


def _card(item: dict, *, owner_console: bool = True) -> dict:
    """One rendered card: the item, plus the words and the routes for its verbs.

    ⚠️ An app decision is INSTANCE-wide, so ``inbox._decide`` refuses it for
    every multitenant caller. Drawing Approve/Reject there anyway is a card
    whose only two buttons answer 403 — so when *owner_console* is false an
    app card carries NO verbs and one sentence saying where the decision is
    taken. The item is still SHOWN: what is waiting is a fact for any seat,
    and hiding it would be the opposite failure.

    ``owner_console`` defaults to True so the pure renderer keeps its old
    behaviour for a caller that does not know the posture; the page passes the
    real ``webgate.is_owner_console()``.
    """
    from webview.inbox import _APP_KINDS  # the ONE list of instance-wide kinds
    kind = item.get("kind") or ""
    if kind in _APP_KINDS and not owner_console:
        row = dict(item)
        row["rendered_actions"] = []
        row["decide_elsewhere"] = t("inbox.decide_owner_console")
        return row
    actions = []
    for i, action in enumerate(item.get("actions") or ()):
        key = _ACTION_WORDS.get((kind, action), f"inbox.action.{action}")
        actions.append({
            "label": t(key),
            "verb": _ACTION_VERB.get(action),
            "primary": i == 0 and action in ("approve", "fulfill"),
        })
    row = dict(item)
    row["rendered_actions"] = actions
    return row


def _pill(count: int, uncertain: bool, unreadable: int) -> tuple:
    """``(class, words)`` for the page's own state pill."""
    if uncertain:
        lists = max(1, unreadable)
        if lists == 1:
            if count == 1:
                return ("is-unknown", t("inbox.pill_partial_one"))
            return ("is-unknown", t("inbox.pill_partial_one_list", count=count))
        return ("is-unknown", t("inbox.pill_partial", count=count, lists=lists))
    if count == 0:
        return ("is-running", t("inbox.pill_none"))
    if count == 1:
        return ("is-needs-you", t("inbox.pill_waiting_one"))
    return ("is-needs-you", t("inbox.pill_waiting", count=count))


def _sources_sentence(body: dict) -> str:
    """What the page read, and what it could not. This is what makes the count
    a claim rather than a number."""
    from core.surfaces.inbox import SOURCE_LABELS
    sources = body.get("sources") or {}
    refused = body.get("unreadable_sources") or []
    read = [SOURCE_LABELS.get(n, n) for n in sources if n not in refused]
    parts = []
    if read:
        parts.append(t("inbox.sources.lead", sources=", ".join(read)))
    if refused:
        parts.append(t("inbox.sources.refused",
                       sources=", ".join(SOURCE_LABELS.get(n, n) for n in refused)))
    elif read:
        parts.append(t("inbox.sources.all_answered"))
    parts.append(t("inbox.sources.what_counts"))
    return " ".join(parts)


def _shared_refusal(body: dict, refused: list) -> str:
    """The ONE reason behind every refused source, when there is only one.

    ⚠️ The reason lives in ``sources[name] = "unreadable(<why>)"`` and nothing
    rendered it — so an unbound console said WHICH lists it could not read and
    never WHY, which is the actionable half. Only shown when every refusal
    shares one reason: five different failures do not collapse into a sentence,
    and inventing one would be the confident answer this page refuses.

    043 A36: this used to slice the composer's prose here, at the render site —
    an undeclared parser in a function whose job is wording. The parse lives
    ONCE in ``webview.inbox.source_reasons`` and this reads the typed mapping.
    """
    reasons = {str((body.get("source_reasons") or {}).get(name) or "")
               for name in refused}
    reasons.discard("")
    if len(reasons) != 1:
        return ""
    return reasons.pop()


def _inbox_context(request: Request) -> dict:
    body = _inbox_summary(request)
    ctx = _shell_context(request, "inbox")
    refused = body.get("unreadable_sources") or []
    count = int(body.get("count") or 0)
    uncertain = bool(body.get("uncertain"))
    pill_class, pill_words = _pill(count, uncertain, len(refused))
    from core.surfaces.inbox import SOURCE_LABELS
    ctx.update({
        "partial_reason": _shared_refusal(body, refused),
        "items": [_card(i, owner_console=ctx["owner_console"])
                  for i in body.get("items") or ()],
        "not_blocking": [_card(i, owner_console=ctx["owner_console"])
                         for i in body.get("not_blocking") or ()],
        "inbox_count": count,
        "inbox_uncertain": uncertain,
        "pill_class": pill_class,
        "pill_words": pill_words,
        "partial_body": (t("inbox.partial.body",
                           source=", ".join(SOURCE_LABELS.get(n, n) for n in refused))
                         if refused else ""),
        "sources_note": _sources_sentence(body),
        "page_glyph": _GLYPH["inbox"],
    })
    return ctx


def _work_context(request: Request) -> dict:
    """Work › Log. The stream itself is drawn client-side from the tenant-scoped
    ``/api/webgate/log`` reader (``webview/worklog_api.py``); this builds only the
    frame around it. ``worklog_classes`` is ``core.activity_class.CLIENT_CLASSES``
    so the filter chips can never drift from the classifier."""
    from core.activity_class import CLIENT_CLASSES
    ctx = _shell_context(request, "work")
    ctx["page_glyph"] = _GLYPH["work"]
    ctx["worklog_classes"] = ",".join(CLIENT_CLASSES)
    return ctx


def _agent_context(request: Request) -> dict:
    """The Agent destination (043 WS-AF1). Six tabs drawn client-side from the
    tenant-scoped Agent readers; this builds only the frame plus the four posture
    axes, computed once from ``build_posture_card`` so a person reads one plain
    sentence per axis rather than the two vocabularies ``doctor`` and
    ``/autonomy`` print on one screen today. Each axis carries its EFFECTIVE
    value (an unreadable axis reads as empty, never a fabricated state)."""
    ctx = _shell_context(request, "agent")
    ctx["page_glyph"] = _GLYPH["agent"]
    try:
        from core.config_policy.posture_card import build_posture_card
        by_env = {r.get("env"): r for r in build_posture_card()}
    except Exception:
        by_env = {}
    posture = {}
    for key, env in (("local", "POLYROB_LOCAL"), ("mode", "AUTONOMY_MODE"),
                     ("loop", "AUTONOMY_POSTURE"), ("compute", "AGENT_COMPUTE_POSTURE")):
        posture[key] = str((by_env.get(env) or {}).get("effective", ""))
    ctx["posture"] = posture
    return ctx


# --- money readers (043 A37): open bridges + on-chain creations ------------- #
# JSON, read-only, tenant-scoped, mounted in BOTH UIs (on ``api_router``). They
# read the SAME builders every other money/status seat reads — ``open_bridges``
# and the status snapshot's creations section — so the console can never disagree
# with the terminal about what is in flight or what the agent has made.

def _bridges_body(user_id: str) -> dict:
    """Open cross-chain bridges for *user_id* — state, age, chain NAME, and an
    ORIGIN explorer link.

    ⚠️ ``tx_ref`` is the ORIGIN transaction, so it is linked to the ORIGIN chain
    and NEVER the destination — putting an origin hash on the destination
    explorer is the exact defect ``bridge_guard`` carries ``origin_chain_id`` to
    prevent. A Solana origin's stored id is the provider's own pseudo id, which
    resolves to no registry name, so ``origin_chain`` is then None and the link
    is omitted rather than pointed at the wrong chain.

    Honest states: an unreadable store is NAMED (``unreadable``), never a
    confident empty list. A genuinely empty board (no bridge ever recorded) is
    ``bridges: []`` with ``unreadable: None``.
    """
    from core.wallet.bridge_guard import chain_name_for_id, open_bridges
    from core.wallet.chains import explorer_url
    try:
        rows = open_bridges(str(user_id))
    except Exception as exc:
        return {"bridges": None, "unreadable": f"{type(exc).__name__}: {exc}"}
    now = time.time()
    out = []
    for r in rows:
        origin_name = chain_name_for_id(r.get("origin_chain_id"))
        tx_ref = r.get("tx_ref")
        origin_link = (explorer_url(origin_name, "tx", tx_ref)
                       if (origin_name and tx_ref) else None)
        created = r.get("created_at")
        try:
            age_sec = max(0.0, now - float(created)) if created else None
        except (TypeError, ValueError):
            age_sec = None
        out.append({
            "id": r.get("id"),
            "state": r.get("state"),
            "origin_chain": origin_name,
            "dest_chain": chain_name_for_id(r.get("dest_chain_id")),
            "currency_out": r.get("currency_out"),
            "amount_usd": r.get("amount_usd"),
            "tx_ref": tx_ref,
            "origin_link": origin_link,
            "age_sec": age_sec,
            "created_at": created,
        })
    return {"bridges": out, "unreadable": None}


def _creations_body(user_id: str) -> dict:
    """What the agent has CREATED on-chain for *user_id* — reuses the status
    snapshot's creations section (over ``wallet_spend`` creation verbs), which
    already carries each item's explorer link.

    Honest states: an unreadable telemetry store renders ``unavailable`` with its
    reason and ``creations: None``, never a confident empty list — "created
    nothing" and "cannot see what I created" are different facts.
    """
    from core.status_snapshot import _creations_section, _guarded
    sec = _guarded("creations", _creations_section, str(user_id),
                   webgate.data_dir())
    return {
        "state": sec.state,
        "reason": sec.reason or None,
        "creations": sec.data.get("creations"),
        "unreadable_rows": sec.data.get("unreadable_rows", 0),
    }


@api_router.get("/api/webgate/bridges")
async def api_bridges(request: Request):
    """Open cross-chain bridges for the effective tenant (043 A37). Read-only,
    both UIs. ``_effective_user_id`` resolves (and 403s) OUTSIDE the reader, so a
    missing tenant is never swallowed into an empty list."""
    from fastapi.responses import JSONResponse
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)  # 403 in multitenant / unbound own_ops
    return JSONResponse(_bridges_body(str(user_id)))


@api_router.get("/api/webgate/creations")
async def api_creations(request: Request):
    """What this agent has deployed or launched on-chain, for the effective
    tenant (043 A37). Read-only, both UIs; tenant resolved and 403'd by
    ``_effective_user_id`` outside the reader."""
    from fastapi.responses import JSONResponse
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    return JSONResponse(_creations_body(str(user_id)))


def _moves_body(user_id: str) -> dict:
    """Recent wallet_spend moves for *user_id* — the middle tier of Money › Moves.

    ⚠️ 043 A33 (closed 2026-09-21): this reader used to carry its OWN SQL over
    ``telemetry_events``. ``core.status_snapshot.moves_section`` is the ONE
    reader of that ledger — the same rows the status snapshot renders on every
    other seat — so a query here was a second answer to one question, free to
    drift from the terminal's. The console now renders that section and only
    RESHAPES it for the pane.

    What the reshape does, and why each part is not the section's job:

    * creation verbs are dropped, because Money › Moves draws them as their own
      tier (``/api/webgate/creations``) and would otherwise draw each twice;
    * the keys are the pane's (``amount_usd`` / ``counterparty``), which the
      money module and its tests read.

    Honest states are the section's, unchanged: an unreadable store is NAMED
    (``unavailable``) and ``moves`` is ``None``, never a confident empty list;
    an unparseable row is COUNTED (``unreadable_rows``), never dropped.

    ⚠️ The event carries no "who decided" lane today (annex A36), so this reader
    does not fabricate one; the console names that gap rather than guessing.
    """
    from core.status_snapshot import (CREATION_VERBS, STATE_UNAVAILABLE,
                                      _guarded, moves_section)
    # `limit` is the section's OUTPUT window; creations are removed after it, so
    # ask for the whole 400-row read and cap the pane's own list below. A
    # smaller limit here would silently shorten the list by however many
    # creations the window happened to contain.
    sec = _guarded("moves", moves_section, str(user_id), webgate.data_dir(),
                   limit=400)
    if sec.state == STATE_UNAVAILABLE:
        return {"moves": None, "unreadable_rows": sec.data.get("unreadable_rows", 0),
                "unavailable": (sec.reason or "unavailable")[:200]}
    out = []
    for item in sec.data.get("moves") or ():
        if item.get("action") in CREATION_VERBS:
            continue  # creations are their own tier
        out.append({
            "action": item.get("action"),
            "amount_usd": item.get("usd"),
            "chain": item.get("chain"),
            "counterparty": item.get("to"),
            "asset": item.get("asset"),
            "tx": item.get("tx"),
            "url": item.get("url"),
            "ts": item.get("ts"),
        })
        if len(out) >= 100:
            break
    return {"moves": out, "unreadable_rows": sec.data.get("unreadable_rows", 0),
            "unavailable": None}


@api_router.get("/api/webgate/moves")
async def api_moves(request: Request):
    """Recent wallet_spend moves for the effective tenant (043 N2). Read-only,
    both UIs; tenant resolved and 403'd by ``_effective_user_id`` OUTSIDE the
    reader, so a missing tenant is never swallowed into a confident empty list."""
    from fastapi.responses import JSONResponse
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    return JSONResponse(_moves_body(str(user_id)))


def _running_dict(row: dict) -> dict:
    """One background delegation row, shaped for Work › Now (043 WS-P1).

    Only the fields the Now view reads: the id, the session it belongs to, the
    goal in plain words, and when it was dispatched (for the elapsed line). The
    result/parent bookkeeping stays in the store."""
    return {
        "delegation_id": row.get("delegation_id"),
        "session_id": row.get("session_id"),
        "goal": row.get("goal"),
        "profile": row.get("profile"),
        "dispatched_at": row.get("dispatched_at"),
        "status": row.get("status"),
    }


@api_router.get("/api/webgate/running")
async def api_running(request: Request):
    """Running background delegations (helpers) for the effective tenant.

    ``AutonomyStateStore.list_running`` is process-wide; this scopes it to the
    caller's ``user_id`` and shapes each row for the Work › Now view. Read-only,
    both UIs. ``_effective_user_id`` resolves (and 403s) OUTSIDE the reader, so a
    missing tenant is never swallowed into a confident empty list; a read error
    is named in ``error`` the way ``pages.api_goals``/``api_cron`` do, never a
    silent empty. Durability off (no store) is an honest ``enabled: false``."""
    from fastapi.responses import JSONResponse
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)  # 403 in multitenant / unbound own_ops
    try:
        from agents.task.agent.autonomy_state import get_autonomy_state_store
        store = get_autonomy_state_store()
        if store is None:
            return JSONResponse({"enabled": False, "running": []})
        rows = [r for r in store.list_running()
                if str(r.get("user_id") or "") == str(user_id)]
        return JSONResponse({"enabled": True,
                             "running": [_running_dict(r) for r in rows]})
    except Exception as exc:
        return JSONResponse({"enabled": True, "running": [],
                             "error": f"{type(exc).__name__}: {exc}"[:200]})


# --- what is in progress right now (2026-09-16 audit, B1) ------------------- #
# Work › Now drew only running GOALS + background delegations, so a console
# beside an agent whose whole day is CRON runs said "Rob is not running anything
# right now." mid-run. This reader names every live actor the stores can PROVE:
# running goals (goals.db), running cron jobs (cron.db — the CAS in cron/jobs.py
# holds ``status='running'`` for the length of a run) and the live sessions the
# shared sqlite session registry knows about, joined to their on-disk task and
# status for THIS tenant only. Every unreadable source is NAMED; ``count`` is
# None when nothing could be read, never a confident zero.

def _sessions_root() -> str:
    """The PathManager session root (``<root>/<user>/<session>/``)."""
    from agents.task.path import pm
    return str(pm().data_root)


def _live_body(user_id: str, data_dir: str, sessions_root: str) -> dict:
    from core.status_live import build_live_status
    return build_live_status(user_id, data_dir, sessions_root)


@api_router.get("/api/webgate/head")
async def api_head(request: Request):
    """The frame's two lines — the head truth and the Inbox badge — rendered by
    the SAME functions the shell used, so ``live.js`` can refresh them without a
    second copy of the words. Tenant-scoped through ``_inbox_state``/``_tenant``
    (a tenant the shell cannot name renders the uncertain badge, as the page
    does); never a 500 — the frame is the one thing that must always answer."""
    count, partial, lists = _inbox_state(request)
    badge, aria = _badge(count, partial, lists)
    return JSONResponse({
        "headline": _headline(request),
        "waiting_line": _waiting_line(count, partial, lists),
        "badge": badge,
        "aria": aria,
        "partial": bool(partial),
    })


@api_router.get("/api/webgate/live")
async def api_live(request: Request):
    """Everything in progress for the effective tenant (running goals, running
    cron jobs, live sessions). Read-only, both UIs; the tenant is resolved (and
    403'd) by ``_effective_user_id`` OUTSIDE the reader so a missing tenant is
    never swallowed into an empty list."""
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    try:
        root = _sessions_root()
    except Exception as exc:
        root = ""
        logger.debug("sessions root probe failed: %s", exc)
    return JSONResponse(_live_body(str(user_id), webgate.data_dir(), root))


# --- Agent readers (043 WS-AB1): capabilities + memory + flags -------------- #
# JSON, read-only, tenant-scoped, mounted in BOTH UIs (on ``api_router``). Each
# row answers the four questions the Agent destination asks of every capability:
# what it is, whether it is on, where it came from, and when it was last used.
# Honest states throughout — an unreadable source is NAMED, never a confident
# empty list. They read the existing SSOTs (``core.tool_capabilities``,
# ``tools.descriptors``, the SkillManager catalog, the MCP config, the profile
# registry, ``core.flags_catalog``), never a second store.


def _tool_risk(tool_id: str) -> str:
    """The catalog's risk tiers, including descriptor aliases."""
    from core.tool_capabilities import CATALOG_ALIASES, high_risk_tool_ids, medium_risk_tool_ids
    canonical = lambda ids: {CATALOG_ALIASES.get(key, key) for key in ids}
    if tool_id in canonical(high_risk_tool_ids()):
        return "high"
    if tool_id in canonical(medium_risk_tool_ids()):
        return "medium"
    return "low"


def _tools_section() -> dict:
    """Every classified tool: what / on / source / capabilities / risk.

    ``on`` is whether the tool CLASS is wired on THIS deploy — a gate-conditional
    tool (``defi_trade``, ``cronjob``, …) only enters ``TOOL_DESCRIPTORS`` when
    its flag is on, so an absent class is an honest "off", never a per-session
    grant. Per-tool "last used" is not tracked anywhere, so it is ``None`` (a
    confident timestamp would be invented)."""
    try:
        from core.tool_capabilities import TOOL_CAPABILITIES
        from tools.descriptors import (
            TOOL_DESCRIPTORS, get_default_tools, get_tool_display_name)
    except Exception as exc:
        return {"items": [], "error": f"{type(exc).__name__}: {exc}"[:200]}
    default = set(get_default_tools())
    by_display = {get_tool_display_name(name): desc
                  for name, desc in TOOL_DESCRIPTORS.items()}
    items = []
    for cap_id in sorted(TOOL_CAPABILITIES):
        caps = sorted(TOOL_CAPABILITIES.get(cap_id) or ())
        desc = by_display.get(cap_id)
        is_default = cap_id in default
        wired = bool(desc and getattr(desc, "tool_class", None) is not None)
        source = ("default" if is_default
                  else "builtin" if (desc and not getattr(desc, "is_optional", True))
                  else "optional")
        items.append({
            "id": cap_id, "kind": "tool",
            "what": (desc.description if desc else None),
            "on": is_default or wired,
            "default": is_default,
            "source": source,
            "capabilities": caps,
            "risk": _tool_risk(cap_id),
            "last_used": None,   # per-tool usage is not tracked — never faked
        })
    return {"items": items, "error": None}


def _skills_section(user_id: str) -> dict:
    """The SkillManager catalog + per-tenant reuse stats (the SAME read
    ``webview/knowledge.py`` does). ``on`` is whether the skill is in the active
    catalog; ``last_used`` is the real ``last_used_at`` from the usage store."""
    catalog, usage, error, usage_error = [], {}, None, None
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        rows = get_skill_usage_store(webgate.data_dir()).list_authored(user_id=user_id)
        usage = {r["skill_id"]: r for r in rows}
    except Exception as exc:
        # ⚠️ 043 A10: this used to become ``{}``, which every row then rendered
        # as "never used" and "0 loads" — a measurement, not the absence of
        # one. The reason rides out so the panel can dash those two columns.
        usage, usage_error = {}, f"{type(exc).__name__}: {exc}"[:200]
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        for m in sm.get_catalog_skills(user_id=user_id, max_skills=200):
            u = usage.get(m.skill_id, {})
            catalog.append({
                "id": m.skill_id, "kind": "skill",
                "what": m.description,
                "on": True,
                "source": m.source,
                "created_by": u.get("created_by", ""),
                "last_used": u.get("last_used_at"),
                "load_count": u.get("load_count", 0),
            })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
    return {"items": (None if error else catalog), "error": error,
            "usage_error": usage_error}


def _mcp_section() -> dict:
    """Configured MCP servers (user/project ``mcp.json``, the file-first SSOT).
    ``on`` is whether the MCP subsystem is enabled on this deploy; a server is
    LISTED even when MCP is off, so the owner can see what is configured."""
    try:
        from tools.mcp.config import load_local_mcp_servers
        servers = load_local_mcp_servers()
    except Exception as exc:
        return {"items": [], "error": f"{type(exc).__name__}: {exc}"[:200],
                "enabled": False}
    from core.bootstrap import _cli_extra_gate
    enabled = _cli_extra_gate("mcp")
    items = []
    for name, cfg in (servers or {}).items():
        detail = None
        if isinstance(cfg, dict):
            detail = cfg.get("command") or cfg.get("url") or cfg.get("transport")
        items.append({"id": name, "kind": "mcp", "what": detail,
                      "on": enabled, "source": "mcp", "last_used": None})
    return {"items": items, "error": None, "enabled": enabled}


def _profiles_section() -> dict:
    """The agent PROFILES ("helpers"/workers, ``data/task/profiles``) — read-only.

    Reads the profile files directly through the registry's ONE dir accessor and
    does NOT create the default set (a read must never seed the store); WS-WK1
    hardens this store, and reading via ``get_profiles_dir`` follows wherever it
    resolves."""
    import yaml as _yaml
    items, error = [], None
    #: 043 A10: a profile file that will not parse is COUNTED, never silently
    #: skipped. "Rob has three helpers" over a directory of five, two of them
    #: broken, is the same confident answer an unreadable store gives.
    unreadable_rows = 0
    try:
        from agents.task.agent.profile_registry import get_profiles_dir
        from agents.task.config import AgentProfileModel
        pdir = get_profiles_dir()
        files = sorted(pdir.glob("*.json")) + sorted(pdir.glob("*.yaml"))
        import json as _json
        for f in files:
            try:
                raw = (_json.loads(f.read_text()) if f.suffix == ".json"
                       else _yaml.safe_load(f.read_text()))
                model = AgentProfileModel(**raw)
            except Exception:
                unreadable_rows += 1
                logger.debug("profile %s could not be parsed", f, exc_info=True)
                continue
            items.append({"id": model.id, "kind": "helper",
                          "what": model.description or model.name,
                          "on": True, "source": "profile", "last_used": None})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
    return {"items": (None if error else items), "error": error,
            "unreadable_rows": unreadable_rows}


def _capabilities_body(user_id: str) -> dict:
    """Skills + tools + MCP + helpers as one capability answer for *user_id*.
    Each section is independently guarded, so one unreadable source degrades to
    its own named error and never blanks the others."""
    return {
        "tools": _tools_section(),
        "skills": _skills_section(str(user_id)),
        "mcp": _mcp_section(),
        "helpers": _profiles_section(),
    }


@api_router.get("/api/webgate/capabilities")
async def api_capabilities(request: Request):
    """Skills/tools/MCP/helpers for the effective tenant (043 AB1). Read-only,
    both UIs; ``_effective_user_id`` resolves (and 403s) OUTSIDE the reader."""
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    return JSONResponse(_capabilities_body(str(user_id)))


async def _memory_search_body(user_id: str, query: str, limit: int) -> dict:
    """Recall (``MemoryProvider.search``) + curated notes for *user_id*.

    ``query`` set → discover; empty → browse-recent (the provider's own rule).
    Both legs are tenant-scoped by the provider. Honest states: no provider is a
    named ``unavailable`` (never an empty list read as "nothing remembered"), and
    a failed recall or notes read is NAMED in its own ``*_error`` field."""
    from webview.pages import _memory_provider, _split_snippets
    provider = _memory_provider()
    mode = "search" if (query or "").strip() else "browse"
    if provider is None:
        return {"provider": None, "mode": mode, "recall": [], "notes": [],
                "recall_error": None, "notes_error": None, "unavailable": True}
    recall, recall_error = [], None
    try:
        raw = await provider.search(query or "", user_id=user_id, limit=limit)
        recall = _split_snippets(raw)
    except Exception as exc:
        recall_error = f"{type(exc).__name__}: {exc}"[:200]
    notes, notes_error = [], None
    if hasattr(provider, "note_list"):
        try:
            rows = await provider.note_list(user_id, status="active", limit=limit)
            q = (query or "").strip().lower()
            for n in rows:
                hay = f"{n.get('content') or ''} {n.get('title') or ''}".lower()
                if q and q not in hay:
                    continue
                notes.append({"id": n.get("id"), "title": n.get("title"),
                              "content": n.get("content"),
                              "tags": n.get("tags") or [],
                              "updated_ts": n.get("updated_ts")})
        except Exception as exc:
            notes_error = f"{type(exc).__name__}: {exc}"[:200]
    return {"provider": getattr(provider, "name", None), "mode": mode,
            "recall": recall, "notes": notes, "recall_error": recall_error,
            "notes_error": notes_error, "unavailable": False}


@api_router.get("/api/webgate/memory/search")
async def api_memory_search(request: Request, query: str = "", limit: int = 10):
    """Recall + curated notes for the effective tenant (043 AB1). Read-only, both
    UIs. A distinct path from the legacy ``GET /api/webgate/memory`` (a recall-
    only reader kept for the legacy console) — a second GET at that path would be
    a silently-dead route the mount clash-check drops."""
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    return JSONResponse(await _memory_search_body(str(user_id), query, limit))


def _flag_guarded(name: str) -> bool:
    """Whether *name* is a flag no remote surface may write — owner binding,
    money bounds, approval, trust posture, any secret (the ONE predicate,
    ``config_service.is_console_unwritable``). Fail-CLOSED (a broken probe marks
    it guarded rather than falsely writable)."""
    try:
        from core.config_service import is_console_unwritable
        return bool(is_console_unwritable(name))
    except Exception:
        return True


def _flag_secret(name: str) -> bool:
    try:
        from core.flags import is_secret_flag
        return bool(is_secret_flag(name))
    except Exception:
        return True


def _flag_groups() -> list:
    try:
        from core.flags_catalog import CATALOG
        return sorted({row[1] for row in CATALOG})
    except Exception:
        return []


def _flags_body(q: str, group: str) -> dict:
    """The env-flag catalog, SEARCH-FIRST: nothing until a query or a group is
    given (the ~689-row catalog is not a first screen). Each hit carries the
    documented default + description and is MARKED ``guarded``/``secret`` so the
    owner knows before trying which flags the console can never write."""
    q = (q or "").strip()
    group = (group or "").strip()
    if not q and not group:
        return {"flags": [], "count": 0, "queried": False, "groups": _flag_groups()}
    try:
        from core.flags_catalog import CATALOG
    except Exception as exc:
        return {"flags": [], "count": 0, "queried": True, "groups": [],
                "error": f"{type(exc).__name__}: {exc}"[:200]}
    from webview.config_view import flag_metadata
    ql = q.lower()
    out = []
    for name, grp, default, desc in CATALOG:
        if group and grp != group:
            continue
        if ql and ql not in name.lower() and ql not in (desc or "").lower():
            continue
        out.append({"name": name, "group": grp, "default": default,
                    "description": desc, "guarded": _flag_guarded(name),
                    "secret": _flag_secret(name), **flag_metadata(name)})
    return {"flags": out, "count": len(out), "queried": True,
            "groups": _flag_groups()}


@api_router.get("/api/webgate/flags")
async def api_flags(request: Request, q: str = "", group: str = ""):
    """The env-flag catalog for the owner console (043 AB1), search-first.

    Read-only, both UIs. ``_effective_user_id`` gates access (403 in multitenant
    without identity, 403 on an unbound own_ops console) even though the catalog
    itself is instance-wide, so the flag reference cannot be read by a caller who
    may not read the owner's console."""
    from webview.pages import _effective_user_id
    _effective_user_id(request)
    return JSONResponse(_flags_body(q, group))


@api_router.get("/api/webgate/chats")
async def api_chats(request: Request, offset: int = 0, limit: int = 50):
    """Lightweight paginated chat summaries; same catalog scope, no feed scan."""
    from webview.pages import _effective_user_id
    from webview.server import _catalog_scope, _annotate_runtime, pm
    from webview.session_catalog import session_page
    _effective_user_id(request)
    scope, user_id = _catalog_scope(request)
    from starlette.concurrency import run_in_threadpool
    body = await run_in_threadpool(session_page, pm().data_root, scope, user_id,
                                  offset=max(0, offset), limit=max(1, min(100, limit)))
    body["sessions"] = _annotate_runtime(body["sessions"])
    return JSONResponse(body)


# --- the destinations ------------------------------------------------------- #
# Registered for every posture. The shell is the frame, not a capability: what
# a given posture may DO inside it is decided per action, as it already is.

def _public_visitor(request: Request) -> bool:
    """A stranger on a posture that answers strangers.

    ⚠️ ``/`` is not only the console index: on ``own_ops``/``multitenant`` it is
    also the PUBLIC STATUS PAGE an unauthenticated visitor sees
    (``webview/server.py::index``). Replacing the index must not put the owner's
    chat in front of a stranger, so the same two-line rule is applied here and
    pinned by a test. Fail-CLOSED: a probe that raises is treated as a stranger.
    """
    try:
        from utils.auth_utils import is_authenticated
        if webgate.posture() == "local":
            return False
        return not is_authenticated(request)
    except Exception:
        logger.warning("visitor probe failed — treating as a stranger", exc_info=True)
        return True


def _status_page(request: Request):
    """The public status page — the same template, from the environment that
    already has the globals every legacy page reads
    (``webview/template_globals.py``). Rendering it from THIS module's own
    minimal environment would be a second, subtly different console."""
    import os
    from core.instance import resolve_instance_id
    from core.version import get_version
    from webview.pages import _TEMPLATES as legacy_templates
    return legacy_templates.TemplateResponse(request, "status.html", {
        "request": request,
        "instance_id": resolve_instance_id(),
        "version": os.environ.get("WEBVIEW_VERSION", get_version()),
    })


def _chat(request: Request, session_id: str = ""):
    """One chat page — cold open when unbound, the thread when bound.

    ⚠️ A caller who does not own a BOUND session gets a 404, not a read-only
    render: this is the owner's seat, and 043's posture rule is that what a
    caller may not have is ABSENT rather than access-denied. A 404 also declines
    to confirm that the session exists.

    ⚠️ A failed ownership PROBE is NOT that. It knows neither that the session
    is gone (which a 404 asserts) nor that the caller may look (which the thread
    asserts), so it renders the frame saying so — no composer, no thread script.
    """
    from webview.chat_open import chat_context
    ctx = _shell_context(request, "new")
    ctx.update(chat_context(request, session_id))
    if session_id and not ctx.get("is_owner") and not ctx.get("ownership_unknown"):
        raise HTTPException(status_code=404)
    return _TEMPLATES.TemplateResponse(request, "chat.html", ctx)


@router.get("/", response_class=HTMLResponse, name="console_new")
async def new(request: Request):
    if _public_visitor(request):
        return _status_page(request)
    return _chat(request)


@router.get("/c/{session_id}", response_class=HTMLResponse, name="console_chat")
async def chat(request: Request, session_id: str):
    """One bound session. The id is validated BEFORE it reaches a template or a
    path — an id that is not one is not a 500, it is a 404."""
    from webview.chat_open import valid_session_id
    if not valid_session_id(session_id):
        raise HTTPException(status_code=404)
    if _public_visitor(request):
        return _status_page(request)
    return _chat(request, session_id)


@router.get("/inbox", response_class=HTMLResponse, name="console_inbox")
async def inbox(request: Request):
    return _TEMPLATES.TemplateResponse(request, "inbox.html",
                                       _inbox_context(request))


@router.get("/work", response_class=HTMLResponse, name="console_work")
async def work(request: Request):
    return _TEMPLATES.TemplateResponse(request, "work.html", _work_context(request))


@router.get("/money", response_class=HTMLResponse, name="console_money")
async def money(request: Request):
    """Money › Book. The book itself is drawn client-side by money.js from the
    tenant-scoped ``/api/webgate/book`` reader; this builds only the frame. The
    verdict leads, because a wrong number here costs real money."""
    return _TEMPLATES.TemplateResponse(request, "money.html",
                                       _shell_context(request, "money"))


@router.get("/agent", response_class=HTMLResponse, name="console_agent")
async def agent(request: Request):
    """Agent — one screen, six tabs (043 WS-AF1). Every pane is drawn client-side
    by agent.js from the tenant-scoped Agent readers; this builds only the frame
    and the four posture axes."""
    return _TEMPLATES.TemplateResponse(request, "agent.html", _agent_context(request))


# --- creating work (043 A5) ------------------------------------------------- #
# The console can now CREATE a goal or a cron job, not only steer an existing
# one. Both writes go through the ONE owner-create SSOT (``core.owner_create``),
# so the console, ``polyrob goals create`` and Telegram ``/trade`` cannot drift
# on the owner-grant policy: the tools named are written VERBATIM (the owner IS
# the operator grant), never the agent's self-grant allowlist. These routes
# WRITE a row; they never run a money verb — a dispatcher runs the work later
# under the same caps, forged/leaf/tainted refusals, the 031 pause and the owner
# queue as any other. The mutation guard (``MUTATION_DEPS``) + the ``console_write``
# audit rail are the same two every mutating console route carries.


async def _json_body(request: Request) -> dict:
    """The request's JSON object, or an empty dict — never a raise."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _as_priority(raw, default: int = 5) -> int:
    """A goal priority clamped to 1-10; a bad value falls back to *default*."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(10, value))


#: The step budget a goal run may be given, matching the agent-callable
#: ``goal_create`` tool's own bounds (``tools/goal_tools.py``, 056 WS5). Omitted
#: means the dispatcher default.
MAX_STEPS_MIN, MAX_STEPS_MAX = 6, 60


def _as_max_steps(raw):
    """``(value, error)`` for an optional step budget.

    ⚠️ REFUSED rather than clamped. A priority is a preference, so silently
    pulling 99 down to 10 loses nothing; a step budget is what the owner
    believes the run will cost, and quietly halving it produces a run that
    stops short for a reason the owner was never told.
    """
    if raw in (None, ""):
        return (None, None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return (None, t("work.create.max_steps_shape"))
    if not (MAX_STEPS_MIN <= value <= MAX_STEPS_MAX):
        return (None, t("work.create.max_steps_range"))
    return (value, None)


@router.post("/api/webgate/goals", dependencies=webgate.MUTATION_DEPS,
             response_class=JSONResponse, name="api_goal_create")
async def api_goal_create(request: Request):
    """Create a goal from the console, with the owner's grant (043 A5)."""
    from agents.task.goals.board import DuplicateGoalError

    from core.owner_create import create_goal
    from webview.pages import _effective_user_id, _webgate_goal_board
    user_id = _effective_user_id(request)
    body = await _json_body(request)
    title = str(body.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "message": t("work.create.goal_no_title")},
                            status_code=400)
    tools = body.get("tools")
    if tools is not None and not (isinstance(tools, list)
                                  and all(isinstance(x, str) for x in tools)):
        return JSONResponse({"ok": False, "message": t("work.create.tools_shape")},
                            status_code=400)
    max_steps, steps_error = _as_max_steps(body.get("max_steps"))
    if steps_error:
        return JSONResponse({"ok": False, "message": steps_error},
                            status_code=400)
    try:
        goal = create_goal(
            _webgate_goal_board(), user_id=user_id, title=title,
            body=str(body.get("body") or ""),
            priority=_as_priority(body.get("priority")),
            tools=tools,
            acceptance=(str(body["acceptance"]) if body.get("acceptance") else None),
            # The dispatcher reads ``payload.max_steps``; ``extra_payload`` is
            # the owner-create helper's own seam for exactly this, so no new
            # parameter and no second writer.
            extra_payload=({"max_steps": max_steps} if max_steps else None),
        )
    except DuplicateGoalError as exc:
        return JSONResponse({"ok": False, "message": t("work.create.goal_duplicate"),
                             "match_id": exc.match_id}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": t("work.create.goal_invalid"),
                             "detail": str(exc)}, status_code=400)
    console_write(CONSOLE_GOAL_CREATE, user_id=user_id, attrs={"goal_id": goal.id})
    return JSONResponse({"ok": True, "id": goal.id, "status": goal.status,
                         "message": t("work.create.goal_ok")}, status_code=201)


@router.post("/api/webgate/cron", dependencies=webgate.MUTATION_DEPS,
             response_class=JSONResponse, name="api_cron_create")
async def api_cron_create(request: Request):
    """Schedule a cron job from the console, with the owner's grant (043 A5).

    ``via="webview"`` is passed to ``CronService.schedule`` so Q2's A29 service
    audit names the surface, alongside the ``console_write`` console-action row.
    """
    import os

    from cron.jobs import CronJobStore
    from cron.service import CronService, ScheduleError

    from core.owner_create import create_cron
    from webview.pages import _data_dir, _effective_user_id
    user_id = _effective_user_id(request)
    body = await _json_body(request)
    task = str(body.get("task") or "").strip()
    if not task:
        return JSONResponse({"ok": False, "message": t("work.create.cron_no_task")},
                            status_code=400)
    schedule_spec = str(body.get("schedule") or "").strip()
    if not schedule_spec:
        return JSONResponse({"ok": False, "message": t("work.create.cron_no_schedule")},
                            status_code=400)
    tools = body.get("tools")
    if tools is not None and not (isinstance(tools, list)
                                  and all(isinstance(x, str) for x in tools)):
        return JSONResponse({"ok": False, "message": t("work.create.tools_shape")},
                            status_code=400)
    service = CronService(CronJobStore(os.path.join(_data_dir(), "cron.db")))
    try:
        job = create_cron(
            service, task=task, schedule_spec=schedule_spec, user_id=user_id,
            tools=tools,
            deliver=(str(body["deliver"]) if body.get("deliver") else None),
            deliver_target=(str(body["deliver_target"])
                            if body.get("deliver_target") else None),
            wake_agent=bool(body.get("wake_agent", True)),
            via="webview",
        )
    except ScheduleError as exc:
        return JSONResponse({"ok": False, "message": t("work.create.cron_bad_schedule"),
                             "detail": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": t("work.create.cron_bad_schedule"),
                             "detail": str(exc)}, status_code=400)
    console_write(CONSOLE_CRON_CREATE, user_id=user_id, attrs={"job_id": job.id})
    return JSONResponse({"ok": True, "id": job.id,
                         "message": t("work.create.cron_ok")}, status_code=201)


# --- Agent writers (043 WS-AB2): SELF-context edit + memory add/forget ------ #
# Reach, not policy. Both writes carry the two guards every mutating console
# route carries (``MUTATION_DEPS`` = read-only + CSRF) and the ``console_write``
# audit rail; neither runs a money verb or changes a gate. They go through the
# EXISTING writers (``SelfContextWriter``, the MemoryProvider's own note store),
# so the console cannot bypass a guard those already enforce.


@router.post("/api/webgate/self-context", dependencies=webgate.MUTATION_DEPS,
             response_class=JSONResponse, name="api_self_context_write")
async def api_self_context_write(request: Request):
    """Propose an edit to the evolving SELF doc (043 AB2).

    ⚠️ ALWAYS a proposal: the write lands in ``.pending`` (``pending=True``), never
    live directly — the owner promotes it through the existing review gate, so a
    console edit follows the same quarantine an agent write does. The SOUL tier
    stays operator-authored and frozen (``self_context_writer.py`` docstring), so
    it is deliberately NOT writable here. The writer runs the full gate — cap,
    the identity safety scan (fail-CLOSED), atomic replace — so a scan-flagged or
    over-cap body is REFUSED, not silently truncated."""
    from core.instance import resolve_instance_id
    from core.self_context_writer import PROVENANCE_USER, SelfContextWriter
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)
    body = await _json_body(request)
    content = str(body.get("content") or "")
    if not content.strip():
        return JSONResponse({"ok": False, "message": t("agent.identity.empty")},
                            status_code=400)
    writer = SelfContextWriter(webgate.data_dir(), instance_id=resolve_instance_id())
    res = writer.propose(content, user_id=str(user_id),
                         created_by=PROVENANCE_USER, pending=True)
    if not res.ok:
        return JSONResponse({"ok": False, "message": t("agent.identity.rejected"),
                             "detail": res.errors}, status_code=400)
    console_write(CONSOLE_SELF_CONTEXT_WRITE, user_id=str(user_id),
                  attrs={"pending": bool(res.pending)})
    return JSONResponse({"ok": True, "pending": bool(res.pending),
                         "message": t("agent.identity.saved")}, status_code=201)


@router.post("/api/webgate/memory", dependencies=webgate.MUTATION_DEPS,
             response_class=JSONResponse, name="api_memory_add")
async def api_memory_add(request: Request):
    """Save one curated note for the effective tenant (043 AB2).

    Through the active MemoryProvider's own note store (tenant-scoped, capped,
    anon-refused by the provider), created by the owner from the console. Reach,
    not policy: no gate is added and no money verb runs."""
    from webview.pages import _effective_user_id, _memory_provider
    user_id = _effective_user_id(request)
    provider = _memory_provider()
    if provider is None or not hasattr(provider, "note_create"):
        return JSONResponse({"ok": False, "message": t("agent.memory.unavailable")},
                            status_code=409)
    body = await _json_body(request)
    content = str(body.get("content") or "")
    if not content.strip():
        return JSONResponse({"ok": False, "message": t("agent.memory.add_failed")},
                            status_code=400)
    title = str(body["title"]) if body.get("title") else None
    tags = str(body["tags"]) if body.get("tags") else None
    note_id = await provider.note_create(str(user_id), content, title=title,
                                         tags=tags, source="webview",
                                         created_by="user", status="active")
    if note_id is None:
        return JSONResponse({"ok": False, "message": t("agent.memory.add_failed")},
                            status_code=400)
    console_write(CONSOLE_MEMORY_WRITE, user_id=str(user_id),
                  attrs={"op": "add", "note_id": note_id})
    return JSONResponse({"ok": True, "id": note_id,
                         "message": t("agent.memory.added")}, status_code=201)


@router.delete("/api/webgate/memory", dependencies=webgate.MUTATION_DEPS,
               response_class=JSONResponse, name="api_memory_forget")
async def api_memory_forget(request: Request):
    """Forget one curated note for the effective tenant (043 AB2).

    ``note_archive`` is a SOFT delete (status→archived): archive-never-delete, so
    the forget is recoverable and the reader (active notes only) stops showing
    it. Tenant-scoped by the provider — a note_id from another tenant is not
    found."""
    from webview.pages import _effective_user_id, _memory_provider
    user_id = _effective_user_id(request)
    provider = _memory_provider()
    if provider is None or not hasattr(provider, "note_archive"):
        return JSONResponse({"ok": False, "message": t("agent.memory.unavailable")},
                            status_code=409)
    body = await _json_body(request)
    raw = body.get("note_id", request.query_params.get("note_id"))
    try:
        note_id = int(raw)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "message": t("agent.memory.not_found")},
                            status_code=400)
    ok = await provider.note_archive(str(user_id), note_id)
    if not ok:
        return JSONResponse({"ok": False, "message": t("agent.memory.not_found")},
                            status_code=404)
    console_write(CONSOLE_MEMORY_WRITE, user_id=str(user_id),
                  attrs={"op": "forget", "note_id": note_id})
    return JSONResponse({"ok": True, "id": note_id,
                         "message": t("agent.memory.forgotten")})


# --- mounting --------------------------------------------------------------- #

def served_paths(app) -> set:
    """Every path *app* already answers, including through an included router.

    ⚠️ ``app.routes`` is NOT the list of paths. Since FastAPI 0.141
    ``include_router`` appends one opaque ``_IncludedRouter`` object that holds
    the real routes on ``.original_router``, and a ``Mount`` holds its own
    ``.routes``. A shallow scan of ``app.routes`` therefore reports a console
    with a dozen included routers as serving four paths, and a clash check
    built on it would be a check that always passes.
    """
    seen = set()

    def walk(routes, depth=0):
        if depth > 6:
            return
        for route in routes or ():
            path = getattr(route, "path", None)
            if isinstance(path, str):
                seen.add(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", ()), depth + 1)
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested, depth + 1)

    walk(getattr(app, "routes", ()))
    return seen


def mount(app) -> None:
    """Add the five destinations to *app* — the ONLY console (043 phase 5).

    ⚠️ **Call this AFTER the app's own route registration**, not in the middle
    of it. The guarantee below is computed from what the app serves at the
    moment of the call, and ``server.py`` registers its console index ``/`` with
    a decorator near the bottom of the module — long after its
    ``include_router`` block.

    A path the app already serves is **left alone**. Starlette matches the
    FIRST registered route, so including a duplicate is not an error: it is a
    route that silently never runs, and which of the two wins depends on the
    order ``server.py`` happens to call things in. ``/`` is exactly that case
    (a legacy console index, if one is still registered): skipping it explicitly
    makes the outcome the same whenever this is called, and says so in the log
    instead of leaving a dead route behind.
    """
    # The JSON endpoints mount FIRST. They are readers and writers the new pages
    # read exactly; they were never behind the (now removed) WEBVIEW_UI switch.
    for one in _API_ROUTERS():
        _mount_one(app, one)
    if _drop_legacy_index(app):
        logger.info("console index replaced by the new chat (043 C6)")
    _mount_one(app, router)


#: The legacy console index's endpoint NAME. Matched as well as the path,
#: because a route at "/" that is not that handler is not ours to delete.
_LEGACY_INDEX_NAME = "index"


def _drop_legacy_index(app) -> bool:
    """Unregister ``server.py``'s console index so the new chat can take ``/``.

    Starlette matches the FIRST registered route, and ``server.py`` registers
    its index long before this runs — so "mount and hope" would leave the config
    form in place and this module's ``/`` silently dead. Removing the one named
    route is the only thing that actually changes what a person sees, and it is
    scoped by NAME so an unrelated handler at ``/`` survives.

    ``/`` is also the public status page, so replacing the index is this route's
    job and the stranger branch is preserved in :func:`_public_visitor`.
    """
    routes = getattr(getattr(app, "router", None), "routes", None)
    if routes is None:
        return False
    victims = [r for r in routes
               if getattr(r, "path", None) == "/"
               and getattr(r, "name", None) == _LEGACY_INDEX_NAME]
    for route in victims:
        routes.remove(route)
    return bool(victims)


def _API_ROUTERS() -> tuple:
    """The routers that answer in BOTH UIs: JSON readers and writers only.

    They ride here rather than in ``webview/server.py`` because they belong to
    the same shell and mounting them from the module that owns them keeps the
    console's mount seam a single call — but they are NOT gated by the page
    switch, which is what that seam had accidentally done to them.
    """
    from webview.inbox import router as inbox_router
    return (inbox_router, api_router)



def served_method_paths(app) -> tuple:
    """``(http, pathless)`` — what *app* answers, keyed by METHOD where it can be.

    ⚠️ A GET and a POST at the same path are two DIFFERENT routes, and Starlette
    matches the path THEN the method. The path-only :func:`served_paths` would
    call a new ``POST /api/webgate/goals`` a clash of the legacy
    ``GET /api/webgate/goals`` and silently drop it — the "route that silently
    never runs" hazard :func:`mount` warns about, made worse by being invisible.
    So HTTP routes are recorded as ``(METHOD, path)`` pairs; a route with no
    ``methods`` (a mount, a websocket) is recorded by its bare path, because a
    structural mount at a path DOES shadow everything under it.
    """
    http = set()
    pathless = set()

    def walk(routes, depth=0):
        if depth > 6:
            return
        for route in routes or ():
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if isinstance(path, str):
                if methods:
                    for method in methods:
                        http.add((str(method).upper(), path))
                else:
                    pathless.add(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", ()), depth + 1)
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested, depth + 1)

    walk(getattr(app, "routes", ()))
    return http, pathless


def _mount_one(app, one) -> None:
    taken_http, taken_pathless = served_method_paths(app)
    taken_any_path = {p for _, p in taken_http} | taken_pathless

    def _clashes(route) -> bool:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            return False
        methods = getattr(route, "methods", None)
        if methods:
            # A method-distinct route (GET vs POST at one path) does NOT clash.
            return path in taken_pathless or any(
                (str(m).upper(), path) in taken_http for m in methods)
        # A mount / websocket shadows the whole path — clash if anyone holds it.
        return path in taken_any_path

    clashes = [r for r in one.routes if _clashes(r)]
    if not clashes:
        app.include_router(one)
        return
    logger.info(
        "console shell: %s already served by this app — left to the existing route",
        ", ".join(sorted(str(getattr(r, "path", "?")) for r in clashes)))
    keep = APIRouter()
    keep.routes.extend(r for r in one.routes if r not in clashes)
    app.include_router(keep)
