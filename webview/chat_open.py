"""The cold open, and the bound session (043 C6).

What a person met on opening this console was a FORM — a model picker, a grid
of tool checkboxes and a max-steps slider — before a single word had been
exchanged. The 2026-09-12 audit called it what it was: "the cold open is a
config form". It is now one true sentence made of real numbers, in the agent's voice,
four things you could ask, and a place to type.

**The sentence is the hard part, and it is the whole point.** Its numbers come
from :func:`core.recap.build_recap` — the same reader ``/journey`` and Telegram
``/recap`` use — so the three seats cannot disagree about what happened. And it
says what it does not know:

* ``build_recap`` emits **no ledger entry at all** for an all-zero rollup AND
  for a rollup it could not read (``core/recap.py`` — "an all-zero ledger is
  NOT activity"). Those two are indistinguishable from here, so the hero makes
  **no money claim** rather than printing a confident ``$0.00``.
* A recap that raises renders "I could not read what I did in the last day",
  and the composer is still there. A person who opens the console to type
  should never be stopped by a number.

The money picker and the step slider move to Agent › Settings in phase 4; the
session request carries only the task, so ``resolve_session_provider_model``
picks the model the operator's runtime config already names.
"""

from webview.session_access import http_session_id
import logging
import re

from fastapi import Request

from webview.copy import t

logger = logging.getLogger(__name__)

#: The four things a cold console offers. Copy keys, in render order.
STARTERS = ("chat.starter.can", "chat.starter.away", "chat.starter.cost",
            "chat.starter.schedule")

#: A session id as the path manager writes it. Anything else is not a session,
#: and is refused before it can become a path.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: ``build_recap`` renders the ledger entry as one line of its own making. These
#: read it back. They are pinned by a contract test that goes THROUGH
#: ``build_recap`` rather than past it, so the day that format changes, the test
#: says so instead of the hero quietly losing its numbers.
_INCOME = re.compile(r"Income:\s*\$(-?[\d,]+(?:\.\d+)?)")
_RUNTIME = re.compile(r"runtime\s*\$(-?[\d,]+(?:\.\d+)?)")
#: An episode entry is ``"<kind>:<outcome>[ $spend][ \"task\"]"``.
_EPISODE = re.compile(r"^goal:(\w+)")


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID.match(session_id or ""))


# --- the seams -------------------------------------------------------------- #

def _tenant(request: Request) -> tuple:
    """``(user_id, refusal)`` — the same fail-closed resolution the frame uses,
    including its refusal of an UNBOUND console (whose reads would otherwise be
    scoped to the instance id and report someone else's empty day)."""
    from webview.pages_new import _tenant as shared
    return shared(request)


def _recap(user_id: str):
    """The last day, from the ONE builder every seat reads."""
    from core.recap import build_recap
    from webview import webgate
    return build_recap(user_id, webgate.data_dir(), "24h")


def _ownership(request: Request, session_id: str) -> tuple:
    """``(is_owner, is_authenticated, unknown)`` for a BOUND session.

    The ONE shared check (``webview.server._check_session_ownership``) — the
    same one the legacy session page calls — so this page cannot start
    disagreeing with it about who owns a session.

    ⚠️ ``unknown`` is the third answer, and it exists because the other two are
    both confident. A caller who is genuinely not the owner gets a 404 ("this is
    not yours, and I will not confirm it exists"). A PROBE that raised knows
    neither thing: answering 404 says the session is gone, and rendering the
    thread says the caller may look. So a failed probe renders the frame with a
    sentence that says exactly what happened, no composer, and no script binding
    to a session the check never cleared.
    """
    try:
        from webview.server import _check_session_ownership
        is_owner, _current, _owner = _check_session_ownership(request, session_id)
    except Exception:
        logger.warning("session ownership check unavailable", exc_info=True)
        return (False, False, True)
    try:
        from utils.auth_utils import is_authenticated
        authed = bool(is_authenticated(request))
    except Exception:
        logger.debug("auth probe failed", exc_info=True)
        authed = False
    return (bool(is_owner), authed or bool(is_owner), False)


# --- the sentence ----------------------------------------------------------- #

def _money(text: str) -> float:
    return float(text.replace(",", ""))


def hero_facts(entries) -> dict:
    """``{goals_done, runtime_usd, income_usd, ledger_note}`` from a recap's own
    entries.

    ``runtime_usd``/``income_usd`` are ``None`` when the recap carried no ledger
    entry — the honest "I have no money to report", which the sentence then
    does not try to fill in.

    ⚠️ ``ledger_note`` carries the recap's own ``⚠ …`` annotation
    (``core/recap.py`` H14b: a leg that could not be read, or wallet metering
    off). Without it the hero rendered *"my running cost $0.00, and $12.00 came
    in"* over a rollup whose runtime leg was never read — a confident zero next
    to a real figure, which is the exact failure the annotation exists to stop.
    A note therefore SUPPRESSES the money clause rather than decorating it: the
    numbers behind it are not trustworthy enough to print in a sentence.
    """
    goals = 0
    runtime = income = None
    note = ""
    for entry in entries or ():
        kind = getattr(entry, "kind", "")
        text = getattr(entry, "text", "") or ""
        if kind == "episode":
            match = _EPISODE.match(text)
            if match and match.group(1) == "done":
                goals += 1
        elif kind == "ledger":
            head, sep, tail = text.partition("⚠")
            if sep:
                note = tail.strip()
            got_income = _INCOME.search(head)
            got_runtime = _RUNTIME.search(head)
            if got_income:
                income = _money(got_income.group(1))
            if got_runtime:
                runtime = _money(got_runtime.group(1))
    return {"goals_done": goals, "runtime_usd": runtime, "income_usd": income,
            "ledger_note": note}


def _escape(text: str) -> str:
    """The hero is rendered ``|safe``, and this is the one part of it that comes
    from outside the copy layer."""
    import html
    return html.escape(str(text))


def _val(text: str) -> str:
    """A number, in the face numbers are set in. Monospace is for values only."""
    return '<span class="val">' + text + "</span>"


def _usd(amount: float) -> str:
    return _val(f"${amount:,.2f}")


def _goals_clause(count: int) -> str:
    if count == 0:
        return t("chat.open.goals_none")
    if count == 1:
        return t("chat.open.goals_one")
    return t("chat.open.goals_many", count=_val(str(count)))


def hero_sentence(facts) -> str:
    """The cold open's one line, as safe HTML (the numbers carry their own face).

    ``facts`` of ``None`` means the recap could not be read — say so, rather
    than render a shape made of zeros.
    """
    if facts is None:
        return t("chat.open.hero_unreadable")
    if not int(facts.get("goals_done") or 0) and not facts.get("runtime_usd") \
            and not facts.get("income_usd") and not facts.get("ledger_note"):
        return t("chat.open.hero_idle")
    goals = _goals_clause(int(facts.get("goals_done") or 0))
    runtime = facts.get("runtime_usd")
    income = facts.get("income_usd")
    note = str(facts.get("ledger_note") or "").strip()
    if note:
        # The recap says a leg could not be read. Printing its zeros in a
        # sentence would make the unread leg indistinguishable from a real
        # $0.00, so the money clause is dropped and the reason is stated.
        return (t("chat.open.hero_no_money", goals=goals) + " "
                + t("chat.open.money_unreadable", why=_escape(note)))
    if runtime is None and income is None:
        return t("chat.open.hero_no_money", goals=goals)
    income_clause = (t("chat.open.income_none") if not income
                     else t("chat.open.income_some", amount=_usd(income)))
    return t("chat.open.hero", goals=goals,
             spend=_usd(runtime or 0.0), income=income_clause)


def cold_open_hero(request: Request) -> str:
    """The hero for THIS request, never raising into the page."""
    user_id, refusal = _tenant(request)
    if not user_id:
        logger.debug("cold open has no tenant: %s", refusal)
        return hero_sentence(None)
    try:
        return hero_sentence(hero_facts(_recap(user_id)))
    except Exception:
        logger.warning("cold open recap failed", exc_info=True)
        return hero_sentence(None)


# --- the two contexts ------------------------------------------------------- #

def starters() -> list:
    return [t(key) for key in STARTERS]


#: A session started by one of these is a RUN view, not a chat with the owner
#: (070 W0.12; W2.25 moves it into its job or goal chat).
RUN_CREATORS = frozenset({"cron", "goal", "correspondent"})

#: The REPL's stock task text: not something the owner asked (plan open question 4).
_REPL_STOCK_TASK = "Interactive conversation session."


def _session_folder(session_id: str):
    """The session's folder under its OWNER's id, or ``None``. Never creates one
    (``pm().get_session_root`` would)."""
    from agents.task.path import pm
    paths = pm()
    clean = http_session_id(session_id, paths)
    owner = paths.get_session_user(clean)
    if not owner:
        return None
    for folder in (paths.data_root / owner / clean, paths.data_root / owner / "sessions" / clean):
        if folder.is_dir():
            return folder
    return None


def chat_exists(session_id: str) -> bool:
    """False only when the read ANSWERED that no such chat folder exists. A read that
    fails answers True: the page then shows its own unreadable state (an unreadable
    store is not an empty one)."""
    try:
        return _session_folder(session_id) is not None
    except Exception:
        logger.warning("chat exists: could not look for %s", session_id, exc_info=True)
        return True


def first_line_context(session_id: str) -> dict:
    """``first_line`` (2000 chars), ``creator``, ``run_kind`` for a bound session.

    A failed read leaves all three empty: no invented question.
    """
    empty = {"first_line": "", "creator": "", "run_kind": "", "run_head": ""}
    try:
        folder = _session_folder(session_id)
        if folder is None:
            return empty
        from webview.session_catalog import session_head
        head = session_head(folder, task_chars=2000)
    except Exception:
        logger.warning("chat first line: could not read session %s", session_id, exc_info=True)
        return empty
    if head["unreadable"].get("task"):
        return empty
    creator = str(head.get("creator") or "").strip().lower()
    task = str(head.get("task") or "").strip()
    if task == _REPL_STOCK_TASK:
        task = ""
    run = creator in RUN_CREATORS
    return {"first_line": task, "creator": creator,
            "run_kind": "run" if run else "chat",
            "run_head": t(f"chat.run_head.{creator}") if run else ""}


def is_terminal_chat(session_id: str) -> bool:
    """A chat the terminal owns (104 D1: one chat per seat). A console line to it used to
    fail with no word to the person and mark the live chat ``failed``. Unreadable = False:
    the send path keeps its own errors."""
    return first_line_context(session_id).get("creator") == "cli"


def terminal_chat_refusal(session_id: str):
    """The 409 for a line to a terminal chat, or None. An owner verb ran before this."""
    if not is_terminal_chat(session_id):
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse({"success": False, "error": t("chat.terminal.note", id=session_id)},
                        status_code=409)


def run_live(session_id: str) -> str:
    """``"true"`` / ``"false"`` / ``""`` — is a live process running this session?

    The transcript reads it (``data-run-live``) to tell a turn that is still
    working from one whose process ended before it wrote a closing event: the
    second must not read "Working for …" forever (070 E.13). ``"true"`` when
    THIS process runs it; else the SHARED registry (``session_registry.db``,
    only when ``SESSION_REGISTRY_BACKEND=sqlite`` — the in-process backend
    cannot see the agent service's sessions, so "absent" there means nothing).
    ``""`` = not known: the transcript keeps its old behaviour.
    """
    try:
        import os

        from agents.task.path import pm
        from webview import webgate
        clean = http_session_id(session_id, pm())
        agent = webgate.in_process_task_agent()
        route_fn = getattr(agent, "route_session", None) if agent else None
        if route_fn is not None:
            route = route_fn(clean)
            if route is not None and getattr(route, "is_local", False):
                return "true"
        backend = (os.getenv("SESSION_REGISTRY_BACKEND") or "memory").strip().lower()
        if backend != "sqlite":
            return ""
        from core.status_live import session_is_live
        live = session_is_live(clean, webgate.data_dir())
    except Exception:
        logger.debug("run liveness probe failed for %s", session_id, exc_info=True)
        return ""
    return "" if live is None else ("true" if live else "false")


def _is_local() -> bool:
    from webview import webgate
    return webgate.is_local()


def _viewer_id(request: Request) -> str:
    """The signed-in person's id, so a line they wrote reads "You"."""
    try:
        from webview.pages import _effective_user_id
        return str(_effective_user_id(request) or "")
    except Exception:
        logger.debug("viewer id unavailable", exc_info=True)
        return ""


def chat_context(request: Request, session_id: str = "") -> dict:
    """The template's own half of the context; the frame adds the rest.

    ``is_owner``/``is_authenticated`` ride on ``document.body``'s data
    attributes because that is where ``static/app/chat-open.js`` reads them
    (``ui-utils.js`` was deleted 2026-09-21). Before this they were simply
    absent, and ``chat-open.js`` read the absence as owner+authenticated —
    every viewer got an owner-capable page.
    """
    bound = bool(session_id)
    if not bound:
        is_owner, authed, unknown = (True, True, False)
    else:
        try:
            is_owner, authed, unknown = _ownership(request, session_id)
        except Exception:
            # _ownership already converts a failed PROBE; this catches anything
            # else it might raise, because the alternative is a 500 that tells
            # the owner nothing at all.
            logger.warning("ownership resolution raised", exc_info=True)
            is_owner, authed, unknown = (False, False, True)
    first = (first_line_context(session_id) if bound and is_owner
             else {"first_line": "", "creator": "", "run_kind": "", "run_head": ""})
    return {
        **first,
        "run_live": run_live(session_id) if bound and is_owner else "",
        "session_id": session_id,
        "viewer_id": _viewer_id(request),
        "no_model_key": "chat.open.no_key" if _is_local() else "chat.open.no_model",
        "is_new": not bound,
        "is_owner": is_owner,
        "is_authenticated": authed,
        "ownership_unknown": unknown,
        "hero": "" if bound else cold_open_hero(request),
        "starters": [] if bound else starters(),
        "page_glyph": "✎",
    }
