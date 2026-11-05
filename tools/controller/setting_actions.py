"""093 D1/D2 — ``propose_setting``: the agent proposes the fix, the owner taps once.

Before this the only exit from a missing
capability was prose ("ask whoever manages that tool"), and the owner had to
know the verb. Now the agent hands ONE owner seat line to
``core.setting_proposals.classify``:

* ``card`` — a SETTING card (``core.surfaces.cards.setting_card``) holding the
  line. Confirm runs it on the owner's seat exactly as if the owner typed it, so
  the seat's owner gate, room refusal and the write oracle run again. A card
  adds no authority.
* ``operator`` — the env file, a backend, a unit: never a card. It is filed as
  an OPEN ask on the goal board (the agent's existing "what I need from you to
  unblock work" queue: ``/asks``, ``/inbox``, ``/status``), with the exact env
  line in the ask (``payload.ask_kind = "config_change"``). Not the ``/dev``
  ops inbox: that rail carries only text the OWNER typed, authenticated on the
  owner-turn path, into a root session — agent-written text must not reach it.
* ``refused`` — money, approval, ingress, trust, secrets, unknown shapes.

D4: an OWNER turn only — never an autonomous run, a room, a correspondent's
session, a leaf/sub-agent, or a forged self-wake re-entry.

⚠️ Registry-closure landmine: NO ``from __future__ import annotations`` here.
"""
import json
import logging
import os

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult
# ``payload.ask_kind`` of an operator config change request (the board's kind
# literals live in ``core.goal_vocab``; re-exported here).
from core.goal_vocab import ASK_KIND_CONFIG_CHANGE

logger = logging.getLogger(__name__)


def owner_turn_refusal(controller, execution_context, user_id: str):
    """Why this is not an owner turn that may propose a setting, or None."""
    from tools.controller.card_actions import _refusal
    why = _refusal(controller, execution_context, user_id)
    if why:
        return why
    try:
        from tools.controller.turn_origin import _is_forged_or_autonomous_turn
        if execution_context is None or _is_forged_or_autonomous_turn(execution_context,
                                                                       controller):
            return ("Refused: a setting is proposed only on a live owner turn — never in "
                    "an autonomous run, a re-entry or a sub-agent. Raise ONE owner_ask "
                    "instead.")
    except Exception:
        return "Refused: the turn could not be verified as an owner turn."
    return None


def _current_tools(controller, user_id: str, verb: str, ref: str):
    """The tool list a grant line would REPLACE: the goal's or the job's
    ``payload.tools`` for an id or a unique prefix. ``None`` when it cannot be
    read or the ref does not name exactly one row (the card then says so)."""
    from core.runtime_paths import container_data_home, cron_db_path, goals_db_path
    from core.sqlite_util import execute_retry
    home = container_data_home(getattr(controller, "container", None))
    if verb == "/goal":
        db, sql = goals_db_path(home), ("SELECT id, payload FROM goals WHERE user_id=? "
                                        "AND kind='goal' AND id LIKE ?")
    else:
        db, sql = cron_db_path(home), ("SELECT id, payload FROM cron_jobs WHERE user_id=? "
                                       "AND id LIKE ?")
    try:
        if not os.path.exists(db):
            return None
        rows = execute_retry(db, sql, (user_id, ref + "%"), fetch="all") or []
        rows = [r for r in rows if str(r[0]).startswith(ref)]
        if len(rows) != 1:
            return None
        tools = (json.loads(rows[0][1] or "{}") or {}).get("tools") or []
        return [str(t) for t in tools]
    except Exception:
        logger.debug("propose_setting: current tools unreadable for %s", ref, exc_info=True)
        return None


def _card_body(p, why: str) -> str:
    body = [p.change]
    if why.strip():
        body.append(f"Why: {why.strip()[:400]}")
    if p.risk:
        body.append(p.risk)
    body.append(f"Applies: {p.when} (who: {p.who}).")
    return "\n".join(body)


def _open_config_change(board, user_id: str, line: str, setting: str = ""):
    """The OPEN ``config_change`` ask for this change, or None. The ONE dedupe:
    by the setting (``KEY=value``) — 095 W1c: a member line is MERGED into the
    running override line, so the same change can print another line after a
    restart — else by the exact line. Two different settings are two asks (the
    board's fuzzy title match would merge ``MCP_ENABLED=true`` into
    ``CRON_ENABLED=true``)."""
    from core.goal_vocab import ASK_OPEN
    for ask in board.asks(user_id=user_id, status=ASK_OPEN):
        pl = ask.payload or {}
        if pl.get("ask_kind") != ASK_KIND_CONFIG_CHANGE:
            continue
        if pl.get("line") == line or (setting and pl.get("setting") == setting):
            return ask
    return None


async def _file_for_operator(controller, user_id: str, sid: str, p, why: str) -> ActionResult:
    from tools.controller.owner_ask_action import _board
    try:
        board = _board(controller)
        ask = _open_config_change(board, user_id, p.env_line, p.setting)
        if ask is None:
            # 095 W8 (SC-11): closes-when — the board sweep closes it once the
            # setting is in effect, or at ``expires_at``.
            from core.setting_proposals import CONFIG_ASK_TTL_DAYS
            import time
            expires_at = getattr(board, "_now", time.time)() + CONFIG_ASK_TTL_DAYS * 86400
            ask = board.create_ask(
                user_id=user_id,
                what=f"Operator: set {p.env_line} on the host (env file), then restart",
                why=(why or "").strip()[:600],
                extra_payload={"origin": "agent", "session_id": sid,
                               "ask_kind": ASK_KIND_CONFIG_CHANGE, "line": p.env_line,
                               "setting": p.setting, "expires_at": expires_at},
                force=True)
    except Exception as e:
        logger.warning("propose_setting: the operator request was not filed: %s", e)
        return ActionResult(error=(f"This needs the operator ({p.env_line}), and the request "
                                   f"could not be filed ({type(e).__name__}). Tell the owner "
                                   f"the exact line in one sentence."),
                            include_in_memory=True)
    short = str(ask.id)[:8]
    from core.setting_proposals import CONFIG_ASK_TTL_DAYS
    return ActionResult(
        extracted_content=(
            f"Not a card: {p.env_line} is an operator setting (the env file on the host; "
            f"applies at the next restart). Filed for the operator as ask #{short} — it "
            f"holds the exact line and shows on /asks; it closes by itself once the "
            f"setting is in effect (or after {CONFIG_ASK_TTL_DAYS} days). Tell the owner in ONE sentence: "
            f"\"this needs the operator; I filed it as #{short}\". Do not retry."),
        include_in_memory=True)


def register_setting_actions(controller) -> None:
    def _refusal_for(execution_context):
        from tools.controller.owner_ask_action import _user
        return owner_turn_refusal(controller, execution_context,
                                  _user(controller, execution_context))

    # Read by agent_status(tool=) (``agent_status_action._may_propose``): the
    # view suggests this verb only where this very check passes.
    controller.propose_setting_refusal = _refusal_for
    # 095 W2 (HI-15): a goal/cron run has no owner turn, so propose_setting could
    # only refuse there; its schema stays off (agent_status suggests owner_ask).
    try:
        from core.autonomy_marker import is_autonomous
        if is_autonomous(getattr(controller, "session_id", None)):
            return
    except Exception:
        pass

    class ProposeSetting(BaseModel):
        command: str = Field(..., min_length=5, max_length=600, description=(
            "The owner's line: `/mcp add <id> <https-url>`, `/config set <key> <value>`, "
            "`/goal grant <id> tools=a,b`, `/job edit <id> grant tools=a,b`, "
            "`/model <name> default`, `/groups role telegram _<chat_id> <uid> <admin|blocked|member>` "
            "or `/groups tools telegram _<chat_id> [admin] add|remove <name>`."))
        why: str = Field("", max_length=600, description="One line: why.")

    @controller.registry.action(
        "Propose a setting change to the OWNER as a one-tap card (owner turn only). An "
        "operator-only setting is filed for the operator and you get its id. It also makes "
        "someone a room admin or grants a room a tool. Not for money (propose_action).",
        param_model=ProposeSetting,
    )
    async def propose_setting(params: ProposeSetting, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        from tools.controller.card_actions import _deliver, _group_for, _session_id
        user_id = _user(controller, execution_context)
        why = owner_turn_refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        from core.setting_proposals import CARD, OPERATOR, classify
        p = classify(params.command)
        sid = _session_id(controller, execution_context)
        if p.outcome == OPERATOR:
            return await _file_for_operator(controller, user_id, sid, p, params.why)
        if p.outcome != CARD:
            return ActionResult(error=f"propose_setting refused: {p.reason}. No card was made.",
                                include_in_memory=True)
        from core.setting_proposals import grant_parts, with_current_tools
        parts = grant_parts(p)
        if parts is not None:
            p = with_current_tools(p, _current_tools(controller, user_id, parts[0], parts[1]))
        from core.surfaces import cards
        try:
            card = cards.setting_card(user_id, p.words, title=f"Proposed setting: {p.line}",
                                      body=_card_body(p, params.why), session_id=sid or None,
                                      group_id=_group_for(user_id, sid))
        except ValueError as e:
            return ActionResult(error=f"propose_setting: {e}", include_in_memory=True)
        outcome = await _deliver(controller, user_id, card, sid)
        where = ("on the owner's chat" if outcome == "sent"
                 else "queued for the owner's chat (quiet hours)" if outcome == "queued"
                 else "in the owner's /cards list (no live chat reached)")
        return ActionResult(
            extracted_content=(
                f"Proposed setting card {card.card_id} is {where}: {card.line}. NOTHING is "
                f"changed until the owner taps Confirm; it then applies: {p.when}. Tell the "
                f"owner in one message what is missing, why, and that one tap fixes it. Do "
                f"not say it is done, and do not retry the failing tool before the tap."),
            include_in_memory=True)


__all__ = ["ASK_KIND_CONFIG_CHANGE", "owner_turn_refusal", "register_setting_actions"]
