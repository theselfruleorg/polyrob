"""061 WS-3 — `owner_ask`: a decision an autonomous run needs is an ASK, never a
question in a message.

Controller-level (registered from ``service.py`` like ``avatar_action``, since
``action_registration.py`` is at its size ceiling) so EVERY session can reach it
regardless of loaded tools — the exit-rail cron loads no ``goal`` tool, so
``goal_ask`` was unreachable and it asked "A or B?" in a ``send_message`` that no
run could ever receive an answer to.

Two verbs in one action:
- ``question=…`` raises a durable, deduplicated ask on the board, stamped with
  the rail that asked (``payload.rail_id``), and tells the owner ONCE through the
  delivery rail (recorded in the owner thread with the ask id). The answer rides
  the rail's next run (``agents/task/goals/rail_answers.py``) or the dependent
  goal's ``owner_unblocked``.
- ``answer=…`` (a GENUINE owner turn of the owner tenant only — H04) decides the ask — the "anything from
  the chat" directive applied to asks: typing "A" works, ``/fulfill`` stays.
"""
import logging
import re
from typing import Dict, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)


def _board(controller):
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import container_data_home, goals_db_path
    return GoalBoard(goals_db_path(container_data_home(getattr(controller, "container", None))))


def _user(controller, execution_context) -> str:
    uid = getattr(execution_context, "user_id", None) or getattr(controller, "user_id", None)
    if uid:
        return str(uid)
    from core.identity import resolve_identity
    return resolve_identity()


def _rail(session_id: str) -> tuple:
    """``(rail_id, kind)`` for the calling session, or ``("", "")``."""
    try:
        from agents.task.goals.autonomy_marker import cron_job_for_session, goal_for_session
        job = cron_job_for_session(session_id)
        if job:
            return f"cron:{job}", "cron"
        goal = goal_for_session(session_id)
        if goal:
            return f"goal:{goal}", "goal"
    except Exception:
        pass
    return "", ""


#: An option marker inside the question: ``A)`` / ``(B)`` at a word start.
_OPTION_MARK = re.compile(r"(?:^|(?<=[\s(,;:]))\(?([A-F])\)\s*")
_OPTION_TAIL = re.compile(r"[\s,;:]*(?:\bor\b)?[\s,;:]*$", re.IGNORECASE)


def ask_options(question: str) -> Dict[str, str]:
    """O14: ``{"A": "…", "B": "…"}`` when the question names its options the
    way the tool asks (``A) … or B) …``), else ``{}``. Letters must run A, B, …
    in order and there must be two or more — anything else is prose, not a
    choice, and gets no buttons."""
    marks = list(_OPTION_MARK.finditer(question or ""))
    letters = [m.group(1) for m in marks]
    if len(marks) < 2 or letters != list("ABCDEF"[:len(marks)]):
        return {}
    out: Dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(question)
        text = _OPTION_TAIL.sub("", question[m.end():end]).strip().rstrip("?.").strip()
        if not text:
            return {}
        out[m.group(1)] = text[:200]
    return out


def _is_leaf(execution_context) -> bool:
    return bool(getattr(execution_context, "is_sub_agent", False)) or \
        str(getattr(execution_context, "role", "") or "") == "leaf"


def _owner_answer_refusal(controller, execution_context, user_id: str) -> Optional[str]:
    """H04: ``answer=`` decides an ask, and a later run reads that text as the
    owner's decision. Only a GENUINE owner turn may write it: not a leaf, not a
    forged self-wake / delegation-result re-entry, not a group turn, not an
    autonomous run (all via ``_is_forged_or_autonomous_turn``), and only for
    the owner tenant. Fail-CLOSED."""
    msg = "Refused: only a genuine owner turn can decide an ask."
    try:
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        if execution_context is None or _is_forged_or_autonomous_turn(execution_context, controller):
            return msg
        from core.config_policy import local_mode_enabled
        from core.instance import is_owner_local_safe, resolve_owner_principal
        if not is_owner_local_safe(user_id, owner_principal=resolve_owner_principal(),
                                   local_enabled=local_mode_enabled()):
            return msg + " This tenant is not the owner."
    except Exception:
        return msg
    return None


def autonomous_wait_refusal(controller, session_id: str):
    """``send_message(wait_for_response=True)`` in an autonomous run: the text
    was sent, but a WAIT is impossible (the run ends, no waiter exists, the
    owner's reply lands in a chat session). Return the structured refusal the
    tool rig uses, or None when the session is interactive."""
    try:
        from agents.task.goals.autonomy_marker import is_autonomous
        if not is_autonomous(session_id):
            return None
    except Exception:
        return None
    return ActionResult(
        extracted_content=(
            "Message sent (non-blocking). gated:autonomous_wait — an autonomous run "
            "cannot wait for a reply: the run ends and the owner's answer lands in a "
            "chat session this run never sees. For a DECISION call "
            "owner_ask(question=\"…A) … or B) …\") — it is durable, shown on every owner "
            "seat, and the answer is handed to this rail's next run. Continue with the "
            "conservative reading now."),
        include_in_memory=True,
        metadata={"conversational_reply": True, "gated": "autonomous_wait"})


def register_owner_ask_action(controller) -> None:
    class OwnerAskAction(BaseModel):
        question: Optional[str] = Field(
            None, max_length=600,
            description=("RAISE: the decision or resource you need from the owner, as ONE "
                         "question answerable in a word or two (name the options: 'A) … or "
                         "B) …'). Durable, deduplicated, shown on every owner seat; the answer "
                         "reaches this rail's next run. In an autonomous run, never ask this "
                         "in a message instead."))
        why: str = Field("", max_length=1200, description="RAISE: one line of context.")
        answer: Optional[str] = Field(
            None, max_length=2000,
            description=("ANSWER (owner turn only): the owner's decision in their words. "
                         "Use when the owner answered an open ask in chat."))
        ask_id: Optional[str] = Field(
            None, description=("ANSWER: the ask to decide. Omit when exactly one ask is open."))
        approved: bool = Field(True, description="ANSWER: False records a decline.")

    @controller.registry.action(
        "Ask the OWNER for a decision the durable way, or record the owner's answer to "
        "an open ask. question=… raises (any session, never a leaf); answer=… decides "
        "(a genuine owner turn only). In an autonomous (goal/cron) run, a decision you "
        "need from the owner is an owner_ask, never a question in a message. In a live "
        "chat turn with the owner, offer the options with present_choice instead.",
        param_model=OwnerAskAction,
    )
    async def owner_ask(params: OwnerAskAction, execution_context=None) -> ActionResult:
        from core.surfaces.room_policy import is_public_session
        if is_public_session(getattr(controller, "orchestrator", None)):
            return ActionResult(error="Refused: a room session cannot raise or answer an owner ask.",
                                include_in_memory=True)
        if _is_leaf(execution_context):
            return ActionResult(error="Refused: a leaf/sub-agent reports its blocker to its parent, "
                                      "never to the owner.", include_in_memory=True)
        # H04: a session a correspondent drives (created for one, or tainted by
        # one) must not speak to the owner AS Rob, nor decide an ask. The gate
        # hook blocks `owner_ask` while tainted too; this holds without it.
        from agents.task.session_class import correspondent_facing
        if correspondent_facing(getattr(controller, "orchestrator", None)):
            return ActionResult(error="Refused: a session holding a correspondent's message cannot "
                                      "raise or answer an owner ask.", include_in_memory=True)
        user_id = _user(controller, execution_context)
        sid = str(getattr(execution_context, "session_id", "") or getattr(controller, "session_id", "") or "")
        question = (params.question or "").strip()
        answer = (params.answer or "").strip()
        if question and answer:
            return ActionResult(error="Give either question= (raise) or answer= (decide), not both.",
                                include_in_memory=True)
        if not question and not answer and not params.ask_id:
            return ActionResult(error="owner_ask needs question= (raise) or answer= (decide).",
                                include_in_memory=True)
        try:
            board = _board(controller)
        except Exception as e:
            return ActionResult(error=f"owner_ask unavailable: the goal board could not be opened ({e}).",
                                include_in_memory=True)

        if question:
            if len(question) < 8:
                return ActionResult(error="The question is too short to decide on.", include_in_memory=True)
            # W1: the buy identity gate already asked the owner which contract
            # is real (a tap in /pending). A second, prose question about the
            # same contract is the duplicate the owner reads twice.
            try:
                from tools.defi.token_identity_ask import open_ask_naming
                dup = open_ask_naming(board, user_id, f"{question} {params.why or ''}")
            except Exception:
                dup = None
            if dup is not None:
                return ActionResult(
                    extracted_content=(f"Not raised: the owner is ALREADY asked this in /pending "
                                       f"(token identity ask `{dup.id}`: {dup.title[:120]}). They "
                                       f"decide it with a tap; do NOT message them about it. Skip "
                                       f"the buy and continue."),
                    include_in_memory=True)
            rail_id, rail_kind = _rail(sid)
            from core.goal_vocab import ASK_OPEN
            before = {a.id for a in board.asks(user_id=user_id, status=ASK_OPEN)}
            options = ask_options(question)
            extra = {"origin": "agent", "session_id": sid,
                     "rail_id": rail_id, "rail_kind": rail_kind}
            if options:
                # O14: the options the owner's tap resolves against (/fulfill_<id>_<letter>).
                extra["options"] = options
            ask = board.create_ask(
                user_id=user_id, what=question, why=(params.why or "").strip(),
                extra_payload=extra)
            short = str(ask.id)[:12]
            if ask.id in before:
                return ActionResult(
                    extracted_content=(f"Ask `{ask.id}` is ALREADY OPEN (refreshed, not duplicated). "
                                       f"The owner sees it on every seat; the answer reaches this rail's "
                                       f"next run. Apply the conservative reading now and log the skip "
                                       f"against ask {short}."),
                    include_in_memory=True)
            # Tell the owner ONCE through the one rail; the thread line carries the ask id.
            try:
                from core.surfaces.user_delivery import deliver_user_message
                taps = ""
                if options:
                    from core.surfaces.tappable import ask_option_token
                    taps = "\nTap an answer: " + " · ".join(
                        f"{letter}) {ask_option_token(ask.id, letter)}" for letter in options)
                text = (f"❓ I need your decision (ask {short}): {question}"
                        + (f"\nWhy: {(params.why or '').strip()[:400]}" if (params.why or '').strip() else "")
                        + taps
                        + "\nAnswer here in chat (e.g. \"A\"), or /fulfill "
                        + f"{ask.id} <answer>.")
                await deliver_user_message(getattr(controller, "container", None), user_id, text,
                                           source="owner_ask", session_id=sid or None,
                                           ask_id=str(ask.id))
            except Exception as e:
                logger.debug("owner_ask: owner notice skipped: %s", e)
            return ActionResult(
                extracted_content=(f"Raised owner ask `{ask.id}` ({rail_kind or 'session'} rail). It stays "
                                   f"visible on every owner seat until answered; the answer is handed to "
                                   f"this rail's next run. Do NOT also message the owner about it; "
                                   f"continue with the conservative reading and log skips against ask {short}."),
                include_in_memory=True)

        # --- answer: a GENUINE owner turn only ---
        refusal = _owner_answer_refusal(controller, execution_context, user_id)
        if refusal:
            return ActionResult(error=refusal, include_in_memory=True)
        from core.goal_vocab import ASK_OPEN, has_own_surface
        # A tool approval or a token-identity question is decided by the owner's
        # own tap on /pending — never by an agent turn's answer= (W1).
        open_asks = [a for a in board.asks(user_id=user_id, status=ASK_OPEN)
                     if not has_own_surface(a.payload or {})]
        target = None
        if params.ask_id:
            target = next((a for a in open_asks if a.id == params.ask_id
                           or a.id.startswith(params.ask_id)), None)
            if target is None:
                return ActionResult(error=f"No OPEN ask matches `{params.ask_id}`.", include_in_memory=True)
        elif len(open_asks) == 1:
            target = open_asks[0]
        elif not open_asks:
            return ActionResult(error="There is no open ask to answer.", include_in_memory=True)
        else:
            listing = "\n".join(f"- {a.id[:12]}: {a.title[:120]}" for a in open_asks[:8])
            return ActionResult(
                error=f"{len(open_asks)} asks are open — pass ask_id= to say which:\n{listing}",
                include_in_memory=True)
        ok, unblocked = board.decide_ask(target.id, user_id=user_id, approved=bool(params.approved),
                                         answer=answer or None,
                                         answer_via=f"owner chat turn (session {sid[:12]})")
        if not ok:
            return ActionResult(error=f"Ask `{target.id}` could not be decided (already closed?).",
                                include_in_memory=True)
        rail = (target.payload or {}).get("rail_id") or ""
        return ActionResult(
            extracted_content=(f"Recorded the owner's {'answer' if params.approved else 'decline'} on ask "
                               f"`{target.id[:12]}` ({target.title[:100]})"
                               + (f": {answer[:200]}" if answer else "")
                               + (f". {unblocked} goal(s) unblocked." if unblocked else ".")
                               + (f" The {rail.split(':')[0]} rail reads it on its next run." if rail else "")),
            include_in_memory=True)
