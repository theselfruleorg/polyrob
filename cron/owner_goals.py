"""``/goal`` — the owner acts on ONE goal, from every seat (080 WS-2).

The body every seat runs (proposal 125 S14: it moved below the seats from
``surfaces/telegram/owner_ops.py``; Telegram, the console and the REPL import it).
It lives in ``cron/`` beside ``cron/owner_jobs.py`` because it opens the goal board
(``modules.goals.board``), which ``core`` may not import; the transition rules
are ``core.goal_owner_verbs`` and ``core.goal_suppressions``, never a second copy.
Short ids resolve through the ONE resolver, ``core.short_id.resolve_prefix``.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from core.short_id import FOREVER_FLAGS, resolve_prefix

logger = logging.getLogger(__name__)


def _code(text: Any) -> str:
    return f"`{text}`"


#: How many recent rows a short-id prefix is resolved against (D11/E21).
#: ``list_recent`` is newest-first, so this window holds what an owner is
#: plausibly looking at; a FULL id still resolves through ``board.get``
#: whatever its age.
_ID_WINDOW = 200


def _resolve_goal(board: Any, user_id: str, ref: str):
    """``(goal, None)`` or ``(None, refusal_text)`` for a goal id or prefix.

    ⚠️ D11/E21: this used ``board.list(user_id=…, limit=1000)``, which is the
    DISPATCHER's order (``priority DESC, created_at ASC``). Used as a lookup
    window it evicts the newest low-priority rows first — exactly the eviction
    that made the agent's own ``goal_list`` show the oldest 100 rows and zero
    stream legs on 2026-08-29 — so the owner could be told "no match" for a
    goal he was reading about one message earlier.

    Order of attempts: the FULL id through ``board.get`` (tenant-scoped, any
    age, no window at all), then a prefix over ``list_recent`` (newest-first).
    """
    ref = (ref or "").strip()
    if not ref:
        return None, "Usage: /goal <verb> <id> (see /goals)"
    try:
        exact = board.get(ref, user_id=user_id)
    except Exception as exc:
        logger.warning("goal lookup failed for %r", ref, exc_info=True)
        return None, (f"I could not read the goal board ({type(exc).__name__}: "
                      f"{str(exc)[:100]}) — that is UNKNOWN, not 'no such goal'.")
    from modules.goals.board import KIND_GOAL
    if exact is not None and exact.kind == KIND_GOAL:
        return exact, None
    try:
        recent = [g for g in board.list_recent(user_id=user_id, limit=_ID_WINDOW)
                  if g.kind == KIND_GOAL]
    except Exception as exc:
        logger.warning("goal board list_recent failed", exc_info=True)
        return None, (f"I could not read the goal board ({type(exc).__name__}: "
                      f"{str(exc)[:100]}) — that is UNKNOWN, not 'no such goal'.")
    goal_id, err = resolve_prefix(ref, [g.id for g in recent])
    if err:
        return None, (f"{err} among my {len(recent)} most recent goals — "
                      f"see /goals, or give the full id.")
    return next(g for g in recent if g.id == goal_id), None


def _allow_reply(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal allow <id|title>`` — turn a switched-off goal back on."""
    from core.goal_suppressions import SCOPE_TITLE, allow, match
    ref = " ".join(rest).strip()
    if not ref:
        return ("Usage: /goal allow <id|title> — turns a goal you switched off back "
                "on. /goals lists what is off.")
    hits = []
    # An id (or prefix) of a goal row names its title.
    if " " not in ref:
        goal, err = _resolve_goal(board, user_id, ref)
        if goal is not None:
            hits = match(board.db_path, user_id=user_id, text=goal.title)
    if not hits:
        hits = match(board.db_path, user_id=user_id, text=ref)
    if not hits:
        return f"Nothing switched off matches {_code(ref[:40])} — /goals lists what is off."
    if len(hits) > 1:
        shown = "; ".join(h.label for h in hits[:5]) + (  # cut: the count follows
            f" (+{len(hits) - 5} more)" if len(hits) > 5 else "")
        return f"That matches more than one: {shown} — use more of the title."
    h = hits[0]
    allow(board.db_path, user_id=user_id, scope=h.scope, value=h.key)
    return f"✅ Back ON: “{h.label}”. It can be created again."


_GOAL_USAGE = (
    "Usage: /goal new <what> · /goal <show|retry|pause|stop|allow|grant> <id>\n"
    "  /goal stop <id> stops it once; add --forever so it is never created again.\n"
    "  /goal retry <id> re-queues a blocked or paused goal (a stopped one stays stopped).\n"
    "See /goals for the board.")


def _goals_off_note() -> str:
    """A stored goal only runs while the dispatcher is on — say so (F-5)."""
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.goals_enabled():
            return ""
    except Exception as exc:
        logger.warning("goals enablement probe failed", exc_info=True)
        return (f"\n⚠️ I could not check whether my own work is switched on "
                f"({type(exc).__name__}) — the goal is stored. Check with /status.")
    return ("\n⚠️ My own work is switched off — the goal is stored, but nothing "
            "will run it until it is on (/status says how).")


def _goal_new(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal new <what>`` — a background goal with the owner's grant, the SAME
    ``core.owner_create.create_goal`` the console and the CLI use (F-11)."""
    from modules.goals.board import DuplicateGoalError, SuppressedGoalError
    from core.owner_create import create_goal, cron_options_payload, split_cron_options
    try:   # 085 W3: the same tools=/rig=/target=/chain=/skills= grant as /job new
        rest, options = split_cron_options(rest)
        tools, extra = cron_options_payload(options)
    except ValueError as e:
        return f"❌ {e}"
    text = " ".join(rest).strip()
    if not text:
        return "Usage: /goal new <what to do> — I work on it in the background."
    title, body = text, ""
    if len(text) > 120:
        title, body = text[:117].rstrip() + "…", text
    try:
        goal = create_goal(board, user_id=user_id, title=title, body=body,
                           tools=tools, extra_payload=extra)
    except SuppressedGoalError:
        return ("You switched that goal off for good. Turn it back on first: "
                "/goal allow <title>")
    except DuplicateGoalError as e:
        return (f"I already have a goal much like that: {_code(e.match_id[:8])} "
                f"“{e.match_title}” — /goal show {e.match_id[:8]}")
    except ValueError as e:
        return f"I could not add that goal: {e}"
    return (f"✅ Goal {_code(goal.id[:8])} [{goal.status}]: {goal.title}\n"
            f"Follow it: /goal show {goal.id[:8]}{_goals_off_note()}")


def _goal_grant(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal grant <id> tools=… [rig=…] [target=… chain=…]`` — the owner's
    grant on an existing goal (085 W3: the line an agent's money ``goal_update``
    card carries). Written with the owner seat's actor."""
    usage = "Usage: `/goal grant <id> tools=a,b [rig=<rig>] [target=0x… chain=<network>]`"
    if len(rest) < 2:
        return usage
    goal, err = _resolve_goal(board, user_id, rest[0])
    if err:
        return err
    from core.owner_create import grant_payload
    try:
        patch = grant_payload(rest[1:])
    except ValueError as e:
        return f"❌ {e}\n{usage}"
    patch["owner_granted"] = True
    if not board.update_fields(goal.id, user_id=user_id, payload_patch=patch):
        return f"Goal {_code(goal.id[:8])} is finished or gone — nothing granted."
    granted = ", ".join(f"{k} {v}" for k, v in patch.items()
                        if k not in ("actor", "owner_granted", "target_authored_by"))
    return f"✅ Granted goal {_code(goal.id[:8])}: {granted}."


#: `/goal show` heads: the body and the result inline, the rest one tap away.
_GOAL_BODY_CHARS = 1200
_GOAL_RESULT_CHARS = 1500


def _goal_show(user_id: str, board: Any, goal: Any) -> str:
    """``/goal show`` — what it is, why it is where it is, what it produced, what
    it cost (when a run recorded it) and the session to open (F-12)."""
    from core.goal_owner_verbs import reason
    out = [f"{_code(goal.id[:8])} [{goal.status}] {goal.title}",
           reason(board, goal, user_id)]
    from core.surfaces.more_store import cut_text
    if goal.body and goal.body != goal.title:
        # 096 C5: the body was cut at 400 with no marker. Now the head, and the
        # whole body one tap away.
        out.append(cut_text(user_id, str(goal.body), _GOAL_BODY_CHARS))
    out.append(f"priority {goal.priority} · failures "
               f"{goal.consecutive_failures}/{goal.max_retries}")
    if goal.result:
        out.append("result: " + cut_text(user_id, str(goal.result), _GOAL_RESULT_CHARS))
    cost = (goal.payload or {}).get("cost_usd")
    if cost is not None:
        try:
            out.append(f"cost: ${float(cost):.2f}")
        except (TypeError, ValueError):
            out.append(f"cost: unavailable (unreadable value {cost!r})")
    if goal.session_id:
        out.append(f"session: {goal.session_id}")
    return "\n".join(out)


def goal_reply(user_id: str, data_dir: str, args: List[str],
               board: Optional[Any] = None) -> str:
    """`/goal <verb> …` — the WRITE counterpart to the read-only `/goals`.

    080 WS-2: ``new`` creates; ``retry`` is the ONE re-queue (``ready`` and
    ``resume`` are deleted); ``pause`` writes ``paused``; ``stop`` is once by
    default and ``--forever`` switches the title off — the SAME
    ``core.goal_suppressions.stop_goal`` every path calls.

    ``board`` lets the caller share an already-open GoalBoard (the harness opens
    one per request) instead of opening a second connection to the same file.
    """
    from modules.goals.board import GoalBoard
    from core import goal_owner_verbs as V
    from core.goal_suppressions import stop_goal
    if board is None:
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    verb = (args[0].lower() if args else "")
    rest = args[1:]
    if verb == "allow":
        return _allow_reply(user_id, board, rest)
    if verb == "new":
        return _goal_new(user_id, board, rest)
    if verb == "grant":
        return _goal_grant(user_id, board, rest)
    if verb not in V.OWNER_GOAL_VERBS:
        return _GOAL_USAGE
    forever = any(a in FOREVER_FLAGS for a in rest)
    rest = [a for a in rest if a not in FOREVER_FLAGS]
    if not rest:
        return f"Usage: /goal {verb} <id> (see /goals)"

    goal, err = _resolve_goal(board, user_id, rest[0])
    if err:
        return err
    if verb == "show":
        return _goal_show(user_id, board, goal)
    if verb == "stop":
        res = stop_goal(board, user_id=user_id, goal=goal, forever=forever,
                        reason=" ".join(rest[1:]))
    elif verb == "pause":
        res = V.pause_goal(board, user_id=user_id, goal=goal)
    else:
        res = V.retry_goal(board, user_id=user_id, goal=goal)
    return ("✅ " if res.ok else "") + res.message

#: Goal buttons on the list (2026-11-20 Telegram UX review, H8).
_GOAL_BUTTONS = 6
_GOAL_LABEL = 24


def goal_buttons(user_id: str, data_dir: str, args: List[str],
                 board: Optional[Any] = None) -> list:
    """The next-step buttons (``core.surfaces.nav.NavButton``) of `/goals` (``args``
    empty: the list opens each open goal) and `/goal show <id>` (it retries, pauses
    or stops). Each is the typed line, so a seat with no buttons loses nothing.
    Fail-open: a fault gives no buttons, never a lost reply."""
    from core.surfaces.nav import NavButton
    try:
        if board is None:
            from modules.goals.board import GoalBoard
            from core.runtime_paths import goals_db_path
            board = GoalBoard(goals_db_path(data_dir))
        verb = (args[0].lower() if args else "")
        if not verb:
            from core.goal_board_render import LEAD_STATUSES
            rows = board.list_recent(user_id=user_id, statuses=LEAD_STATUSES,
                                     limit=_GOAL_BUTTONS * 2) or []
            rank = {s: i for i, s in enumerate(LEAD_STATUSES)}   # the list's own order
            rows = sorted(rows, key=lambda g: rank.get(getattr(g, "status", ""), 99))
            out = []
            for g in rows[:_GOAL_BUTTONS]:
                title = " ".join(str(getattr(g, "title", "") or "goal").split())
                out.append(NavButton(title if len(title) <= _GOAL_LABEL
                                     else title[:_GOAL_LABEL - 1] + "…",
                                     f"/goal show {(g.id or '')[:8]}"))
            return out
        if verb == "show" and len(args) > 1:
            goal, err = _resolve_goal(board, user_id, args[1])
            if err or goal is None:
                return []
            short, status = goal.id[:8], getattr(goal, "status", "")
            out = []
            if status in ("paused", "blocked"):
                out.append(NavButton("↻ Retry", f"/goal retry {short}", "primary"))
            if status in ("ready", "running", "waiting", "triage", "blocked"):
                out.append(NavButton("⏸ Pause", f"/goal pause {short}"))
            if status not in ("done", "cancelled"):
                out.append(NavButton("■ Stop", f"/goal stop {short}", "danger"))
            return out + [NavButton("◀ All goals", "/goals")]
    except Exception:
        logger.debug("goal buttons unavailable (fail-open)", exc_info=True)
    return []


def goal_command_reply(user_id: str, data_dir: str, args: List[str],
                       board: Optional[Any] = None) -> Any:
    """`/goal …` with its next-step buttons — the reply every seat that draws
    buttons shows (``CommandReply.actions``); the text is :func:`goal_reply`'s."""
    from core.surfaces.command_reply import with_actions
    if board is None:   # one board for the reply and its buttons (one schema init)
        from core.runtime_paths import goals_db_path
        from modules.goals.board import GoalBoard
        board = GoalBoard(goals_db_path(data_dir))
    text = goal_reply(user_id, data_dir, args, board=board)
    return with_actions(user_id, text, goal_buttons(user_id, data_dir, args, board=board))


__all__ = ["FOREVER_FLAGS", "goal_buttons", "goal_command_reply", "goal_reply"]
