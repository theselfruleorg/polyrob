"""`goal` agent tool (W4 — durable goal board).

Lets an agent record durable, cross-session goals it (or a dispatcher) will pursue
beyond the current turn — distinct from the session-scoped TODO (`task` tool) and
from `cronjob` (time-triggered). Thin glue over :class:`agents.task.goals.board.GoalBoard`;
all state/claim/breaker logic lives there.

Off by default: registered only when ``GOALS_ENABLED=true`` and never in the default
``tool_ids`` list, so production is unaffected until goals are turned on. Uses
``BaseTool.action(param_model=...)`` like ``cronjob_tools`` — NOT bare
``@registry.action`` closures — so ``from __future__ import annotations`` is safe here
(the param model is explicit, not introspected from a stringized annotation).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional, Dict

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from agents.task.loop_types.controller_types import ActionResult
from agents.task.goals.board import DuplicateGoalError, GoalBoard

logger = logging.getLogger(__name__)

# Proposal 001 (owner-approved option 2, 2026-07-01): tools an agent may request for its OWN
# self-created goals, filtered to a safe allowlist so it can self-direct research/content/coding
# (and — per owner — posting) but NOT self-grant money-SPEND (wallet/x402_pay/hyperliquid/
# polymarket), code execution, cron, or meta goal/skill tools.
# Proposal 009 (owner battle-test kickoff, 2026-07-13/14): the kickoff mission explicitly
# sanctions email outreach, telegram group/channel posting (`message` — every send is still
# gated by the owner outbound allowlist) and x402 INVOICING (receivables only, capped;
# x402_pay/spend stays excluded), plus knowledge notes.
# Proposal 029 R5 (2026-08-24): `defi_data` joins the allowlist. It is READ-only
# token sight — it constructs no signer and broadcasts nothing, and it is not in
# the capability table's `money` set — so excluding it was a tax on
# reconnaissance rather than a safety property: every screening goal had to wait
# for an operator-seeded cycle. `defi_trade` stays excluded, and that is exactly
# where the line belongs. (`portfolio` remains separately gated by NAME while
# correspondent-tainted; adding the tool here does not touch that.)
# The ids live in core/config_policy/profiles.py as `ceiling:self_goal` (067 P1).
from core.config_policy.profiles import profile as _profile  # noqa: E402

_SELF_GOAL_ALLOWED_TOOLS = frozenset(_profile("ceiling:self_goal"))


def allowed_self_goal_tools() -> frozenset:
    """Tool ids an agent-created goal may request. Under effective autonomous mode
    (AUTONOMY_MODE=autonomous on a single-owner instance) this expands to the full
    AUTONOMOUS_MODE_TOOLS grant; money-spend/host tools are in NEITHER set."""
    try:
        from agents.task.constants import autonomous_mode_tools
        from core.config_policy import full_autonomy_enabled
        if full_autonomy_enabled():
            # Folds in the defi rail when DEFI_AGENT_AUTONOMY is armed. ⚠️ That
            # DOES mean a goal the agent writes itself can carry `defi_trade`
            # once armed — deliberate, on the owner's 2026-09-12 directive. What
            # bounds an injected goal is the CAP and the per-verb simulation, not
            # the toolset: the per-tx ceiling, the rolling daily cap, the
            # kill-switch, the 031 pause and the owner queue above
            # DEFI_AUTONOMOUS_MAX_USD all still apply. Unarmed, this is the
            # historical set exactly.
            return frozenset(_SELF_GOAL_ALLOWED_TOOLS | set(autonomous_mode_tools()))
    except Exception:
        pass
    return _SELF_GOAL_ALLOWED_TOOLS


def agent_rig_refusal(rig: Optional[str]) -> Optional[str]:
    """The error for a rig a turn that is not an owner seat may not request,
    else None — the same principal rule `tools=` follows
    (:func:`allowed_self_goal_tools`). A rig whose ids leave the ceiling is
    REFUSED (not silently narrowed) so the agent learns which rigs it can use.
    `full` always passes — it means the caller's own default, which is no wider
    than omitting a rig.
    """
    from core.config_policy.rigs import rig_names, rig_tools, rig_ungrantable_ids
    ceiling = allowed_self_goal_tools()
    blocked = rig_ungrantable_ids(rig, ceiling)
    if not blocked:
        return None
    usable = [n for n in rig_names()
              if not [t for t in (rig_tools(n) or []) if t not in ceiling]]
    return (f"Tool rig '{rig}' NOT granted: it requests {', '.join(blocked)}, which "
            f"an agent-created goal or cron job cannot grant itself (only the owner "
            f"can set it, from an owner seat). Rigs you can use: "
            f"{', '.join(usable)}.")


def _is_leaf_context(execution_context) -> bool:
    """A delegated leaf / sub-agent turn (it reports to its parent, never to the owner)."""
    return bool(getattr(execution_context, "is_sub_agent", False)) or \
        str(getattr(execution_context, "role", "") or "") == "leaf"


def owner_seat_turn(execution_context) -> bool:
    """Whether a genuine owner turn may request a standing-work mutation.
    An ACCESS check on the turn, never on the row. Fail closed."""
    from core.security.owner_turn import is_genuine_owner_turn
    return is_genuine_owner_turn(execution_context)


def autonomous_run_may_schedule(execution_context) -> bool:
    """M07, re-keyed on the regime: a goal/cron-dispatched MAIN-agent run on the
    owner tenant may start its own recurring job when the instance runs the
    ``autonomous`` (or ``armed``) regime — that is what `cronjob` in
    AUTONOMOUS_MODE_TOOLS is for. A leaf / sub-agent, a self-wake or
    delegation-result re-entry and a room turn stay refused in every regime.
    Fail-closed on any probe error."""
    if execution_context is None:
        return False
    try:
        from core.config_policy.autonomy_mode import autonomous_work_enabled
        if not autonomous_work_enabled():
            return False
        metadata = getattr(execution_context, "metadata", None) or {}
        if metadata.get("turn_kind") == "group":
            return False
        from tools.controller.turn_origin import _is_autonomous_goal_turn
        if not _is_autonomous_goal_turn(execution_context, None):
            return False
        from core.security.owner_turn import owner_turn_refusal
        return owner_turn_refusal(execution_context, verb="cronjob_schedule",
                                  does="starts recurring work",
                                  public="start recurring work") is None
    except Exception:
        return False


def owner_turn_goal_tools() -> frozenset:
    """What `tools=` may carry on an owner-seat turn: the self-goal ceiling plus
    every id a named rig can request — no wider than the rig an owner turn may
    already pick. Host tools (shell/process/code_execution) and the payment
    tools outside the rig table stay excluded; they ride their own postures."""
    from core.config_policy.rigs import RIGS
    return frozenset(allowed_self_goal_tools()
                     | {t for ids in RIGS.values() for t in ids})


# --- 085 W3 (D3): standing MONEY work from a model call needs the owner's tap ---
#
# ``is_genuine_owner_turn`` says who opened the turn, not what the turn read: a page,
# a mail or a document read in an owner turn could write a goal or job that spends
# every day. So an owner-turn goal/job write that carries a money tool, a money rig
# or a buy target writes NO row: it raises a card holding the owner seat's own line
# (``/goal new …`` / ``/job new …``). The owner's Confirm runs that line on the seat
# (a card adds no authority) and the seat writes it with ``actor="owner_seat"``.
# Work with none of these writes at once, as before.


def money_spend_tools() -> frozenset:
    """The capability table's MONEY ids an agent cannot hold by default: the money
    set minus the self-goal ceiling (``x402_invoice`` only receives; ``defi_data``
    only reads)."""
    from core.tool_capabilities import ids_with
    return frozenset(ids_with("money")) - _SELF_GOAL_ALLOWED_TOOLS


def money_job_reasons(*, tools: Optional[List[str]] = None, rig: Optional[str] = None,
                      target: Optional[Dict[str, str]] = None) -> List[str]:
    """Why a goal/job write is standing MONEY work (085 D3), or ``[]``."""
    spend = money_spend_tools()
    out: List[str] = []
    named = sorted({str(t) for t in (tools or []) if str(t) in spend})
    if named:
        out.append("money tool " + ", ".join(named))
    if rig:
        from core.config_policy.rigs import rig_tools
        in_rig = sorted(set(rig_tools(rig) or []) & spend)
        if in_rig:
            out.append(f"money rig {rig} ({', '.join(in_rig)})")
    if target:
        out.append(f"buy target {target.get('address')} on {target.get('chain')}")
    return out


def prose_words(text: Any) -> List[str]:
    """Free text (a task, a goal title) as job-card line words
    (``core.surfaces.cards.card_words``), with every prose word shaped like a seat
    grant option (``tools=…``, ``target=…``, ``rig=…``) disarmed (``=`` → ``:``):
    on the card line only the grant words this module adds are a grant."""
    from core.owner_create import CRON_OPTION_KEYS
    from core.surfaces.cards import card_words
    out: List[str] = []
    for w in card_words(text):
        key, sep, value = w.partition("=")
        out.append(f"{key}:{value}" if sep and key.strip().lower() in CRON_OPTION_KEYS
                   else w)
    return out


def grant_option_words(*, tools: Optional[List[str]] = None, rig: Optional[str] = None,
                       target: Optional[Dict[str, str]] = None,
                       skills: Optional[List[str]] = None,
                       deliver: Optional[str] = None) -> List[str]:
    """The owner-seat ``key=value`` words (``core.owner_create.CRON_OPTION_KEYS``)
    that carry a job's grant on its card line."""
    words: List[str] = []
    if tools:
        words.append("tools=" + ",".join(sorted(dict.fromkeys(tools))))
    if rig:
        words.append(f"rig={rig}")
    if target:
        words += [f"target={target['address']}", f"chain={target['chain']}"]
    if skills:
        words.append("skills=" + ",".join(skills))
    if deliver:
        words.append(f"deliver={deliver}")
    return words


async def raise_job_card(tool: Any, execution_context, words: List[str], *, what: str,
                         reasons: List[str], details: List[str],
                         not_carried: List[str], later: str) -> ActionResult:
    """Make the D3 card for one standing money write and put it to the owner.
    Writes NO goal or job row. Fail-closed: no card, no row."""
    from core.surfaces import cards
    user_id = tool._user(execution_context)
    sid = str(getattr(execution_context, "session_id", "") or "") or None
    body = [f"This {what} carries {'; '.join(reasons)}, so it waits for your Confirm."]
    body += details
    if not_carried:
        body.append("Not on the line (the agent sets these after you confirm): "
                    + ", ".join(not_carried) + ".")
    try:
        card = cards.job_card(user_id, words, title=f"Standing money work — {what}",
                              body="\n".join(body), session_id=sid)
    except ValueError as exc:
        return ActionResult(error=(f"Nothing was written: this {what} carries "
                                   f"{'; '.join(reasons)}, which needs the owner's Confirm, "
                                   f"and the card could not be made ({exc})."),
                            include_in_memory=True)
    outcome = "failed"
    try:
        from core.surfaces.user_delivery import deliver_user_message
        outcome = await deliver_user_message(
            getattr(tool, "container", None), user_id, cards.render_text(card),
            source="action_card", session_id=sid, card_id=card.card_id)
    except Exception as exc:
        logger.warning("job card %s delivery failed: %s", card.card_id, exc)
    where = ("on the owner's chat" if outcome in ("sent", "queued")
             else "in the owner's /cards list (no live chat reached)")
    tail = f" After the owner confirms, {later}" if not_carried else ""
    return ActionResult(
        extracted_content=(f"NOT written: this {what} carries {'; '.join(reasons)}, so it "
                           f"needs the owner's Confirm. Card {card.card_id} is {where}; it "
                           f"holds the whole line: {card.line}. Confirm runs that line on "
                           f"the owner's seat, which writes it with the owner's grant. Do "
                           f"not say it is created, scheduled or running.{tail}"),
        include_in_memory=True)


# Proposal 009 #1 (2026-07-14): whenever a self-created goal carries ANY tools payload, union in
# this safe baseline so the dispatched session is never starved of basics (night-1 failure mode:
# tools=['twitter'] sessions had no filesystem; text-only goals had no web_fetch to research).
_SELF_GOAL_BASELINE_TOOLS = _profile("baseline:self_goal")

# Proposal 009 option B: when the agent sets no `tools`, infer them from the goal's own text —
# the night-1 blocked goals literally named their needed tool in title/acceptance ("Publish
# queued OSS launch X thread" → twitter) but dispatched tool-starved. Substring match on
# lowercased title+body+acceptance; result is still filtered through _SELF_GOAL_ALLOWED_TOOLS,
# so inference can never grant more than an explicit request could.
_TOOL_TEXT_TOKENS = {
    "twitter": ("twitter", "tweet", "x thread", "x post", "x.com"),
    "email": ("email", "e-mail", "mailbox", "imap", "smtp"),
    "message": ("telegram", "t.me/", "channel"),
    "x402_invoice": ("x402", "invoice", "invoicing"),
    "web_fetch": ("web_fetch", "fetch", "http", "url", "website", "research", "browse"),
    "knowledge": ("knowledge", "notes"),
    "anysite": ("anysite",),
    "coding": ("coding",),
}


def _infer_tools_from_text(*texts: Optional[str]) -> set:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return set()
    found = {tool for tool, tokens in _TOOL_TEXT_TOKENS.items()
             if any(tok in blob for tok in tokens)}
    return found & allowed_self_goal_tools()


# 2026-08-18 (intel finding, MEDIUM-HIGH — recurred across x402/video/micro-app goal
# families): self-created goals that EXPLICITLY narrow `tools` and carry an `http_ok`
# acceptance check are structurally doomed — dispatcher._resolve_goal_tools returns
# `payload.tools` VERBATIM the moment it's non-empty (agents/task/goals/dispatcher.py,
# "if own: return own"), with NO widening for compute posture, and shell/process/
# code_execution can NEVER be added via goal_create in the first place (deliberately
# excluded from both _SELF_GOAL_ALLOWED_TOOLS and AUTONOMOUS_MODE_TOOLS — a security
# boundary: host/compute tools ride AGENT_COMPUTE_POSTURE only, never a self-grant).
# So there is nothing to auto-append here; the only tools that could serve an http_ok
# check are the ones this function is forbidden from ever adding. The fix is to warn
# loudly at create time instead of letting the goal dispatch, burn its step budget
# trying to work around a tool it can never have, and die the identical way as its
# 3+ predecessors in this exact family (evidenced live: rob-status/video-render goals).
_COMPUTE_TOOL_IDS = frozenset({"shell", "process", "code_execution"})


def _dropped_tools_note(requested: Optional[List[str]]) -> str:
    """What the caller asked for and did NOT get, said out loud.

    The filter itself is correct and unchanged — an agent-created goal must
    never be able to acquire a money-SPEND verb, because the turn-origin gate
    treats a genuine autonomous goal turn as allowed, which makes this allowlist
    the only line stopping an INJECTED goal from trading.

    What was wrong is that the strip was SILENT. Dropped ids went to a log line
    the agent never sees, so `goal_create` returned success and the caller
    learned nothing; it then hit the gap at dispatch, filed "defi_trade not
    granted" as an owner ask, and repeated — roughly fifty times across two
    weeks of prod. A boundary the caller cannot see is one it cannot respect,
    and the money case needs the stronger sentence: not "ask again", but "this
    can never come from here".
    """
    if not requested:
        return ""
    allowed = allowed_self_goal_tools()
    dropped = [t for t in requested if t not in allowed]
    if not dropped:
        return ""
    from core.tool_capabilities import ids_with
    money = sorted(set(dropped) & set(ids_with("money")))
    note = ("\n⚠️ NOT granted (a goal you create yourself cannot carry these): "
            f"{', '.join(dropped)}.")
    if money:
        note += (f" {', '.join(money)} " + ("is a money tool" if len(money) == 1
                                            else "are money tools")
                 + " and can NEVER be granted this way — do not retry with "
                 "different wording. Only the owner/operator grants one: on a "
                 "goal THEY seeded, or at session creation. If you have a "
                 "candidate and no grant, write it where the granted run will "
                 "read it, and escalate ONCE rather than every run.")
    return note


def _compute_tool_mismatch_warning(tools: Optional[List[str]],
                                    acceptance_checks: Optional[List[Any]]) -> Optional[str]:
    """None if no mismatch; else a warning string to surface in the create result.

    Only fires when `tools` was explicitly set (narrowing away from the wide
    default) AND an `http_ok` check is present AND none of the compute tools
    are already in the list — i.e. exactly the combination that can never
    succeed, not a mere heuristic guess.
    """
    if not tools:
        return None
    if set(tools) & _COMPUTE_TOOL_IDS:
        return None
    has_http_ok = any(
        isinstance(c, dict) and c.get("type") == "http_ok" for c in (acceptance_checks or [])
    )
    if not has_http_ok:
        return None
    return (
        "⚠️ tool/acceptance mismatch: this goal's acceptance_checks include an "
        "http_ok probe (needs a live HTTP server), but self-created goals can NEVER "
        "be granted shell/process/code_execution — those are host/compute tools "
        "gated separately by AGENT_COMPUTE_POSTURE, not by this tool's `tools` "
        "param. With an explicit tools list set, dispatch uses it VERBATIM with no "
        "widening, so this goal will dispatch without compute tools and cannot "
        "serve HTTP. Recreate this goal WITHOUT the `tools` param (omit it "
        "entirely) so dispatch applies the wide autonomous default, which includes "
        "compute tools at the current posture."
    )


#: An acceptance proved only by the goal board itself (intel 2026-10-17).
_SELF_REFERENTIAL_ACCEPTANCE = re.compile(
    r"\bgoal[ _-]?row\b|\bgoal_show\b|\bgoal_list\b|\bgoal board\b", re.IGNORECASE)


_CADENCE_RE = re.compile(
    r"\b(?:every|each)\s+(?:run|day|morning|evening|night|week|hour|\d+\s*(?:h|hours?|min(?:ute)?s?|days?))\b"
    r"|\bper\s+(?:run|day|week|hour)\b|\bdaily\b|\bweekly\b|\bhourly\b|\bonce\s+a\s+day\b",
    re.IGNORECASE)


def _recurring_work_warning(title: str, body: str, acceptance: str) -> Optional[str]:
    """A warning when a goal's text describes a cadence; None otherwise.

    A goal runs ONCE: it completes when its acceptance passes and nothing runs it
    again. 2026-10-15 the X growth "runner" (every run / one post per day) went
    `done` after its first pacing-hold run and the owner asked why it stopped.
    """
    if not _CADENCE_RE.search(" ".join(t or "" for t in (title, body, acceptance))):
        return None
    return ("⚠️ this reads like RECURRING work, but a goal runs ONCE — it completes "
            "when its acceptance passes and nothing runs it again. For work that "
            "repeats (every run / per day / daily), schedule it with cronjob_schedule "
            "instead, and cancel this goal if it was meant to repeat.")


class GoalCreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., description="Short title for the durable goal.", min_length=3)
    body: str = Field("", description="Full instructions for pursuing the goal (what 'done' looks like).")
    priority: int = Field(5, ge=1, le=10, description="1-10, higher runs first.")
    tools: Optional[List[str]] = Field(
        None,
        description=("ALWAYS list every tool this goal needs to actually finish (e.g. 'twitter' "
                     "to post to X, 'email' to send mail, 'message' to post to telegram, "
                     "'x402_invoice' to invoice, 'web_fetch' to read the web) — a goal without "
                     "the right tools dispatches tool-starved and blocks. Filtered to the "
                     "self-goal ceiling: filesystem, task, web_fetch, knowledge, browser, "
                     "perplexity, mcp, anysite, coding (runs tests, so it executes commands), "
                     "twitter, email, message, x402_invoice, defi_data. Under autonomous mode "
                     "the ceiling widens to the autonomous grant (adds goal, cronjob, x_browser; "
                     "plus defi_trade, launchpad, dapp_browser and x402_pay when "
                     "DEFI_AGENT_AUTONOMY is armed). Host shell/process/code_execution are never "
                     "granted here. Dropped ids are named in the result. If unset, tools are "
                     "inferred from the goal text and a safe baseline is applied."),
    )
    rig: Optional[str] = Field(
        None,
        description=("Optional NAMED tool rig instead of an explicit `tools` list: "
                     "'money_rail', 'social', 'research', 'ops', or 'full'. A narrow rig "
                     "ships far fewer tool schemas on every step of the run, which is most "
                     "of what a goal costs. `tools`, when set, always wins. A rig is a "
                     "REQUEST, not a grant: a rig with an id outside the ceiling described "
                     "under `tools` is REFUSED (e.g. 'money_rail' asks for defi_trade, which "
                     "needs DEFI_AGENT_AUTONOMY armed; 'ops' asks for cronjob, which needs "
                     "autonomous mode)."),
    )
    skills: Optional[List[str]] = Field(
        None,
        description=("Optional skill ids this goal's runs must load (its doctrine, e.g. "
                     "['rh-reporting']). Pinned skills load every run instead of depending on "
                     "keyword matching; they grant no tool. Up to 8; omit to match by keyword."),
    )
    target_token: Optional[Dict[str, str]] = Field(
        None,
        description=("068: the ONE contract this goal's runs may acquire, as data: "
                     "{'chain': 'robinhood', 'address': '0x…'}. Buys of anything else "
                     "are refused. A goal created by a run that already has a target "
                     "inherits it and may only restate the same one."),
    )
    depends_on: Optional[List[str]] = Field(
        None,
        description="Goal ids that must complete first — the goal waits until they are done.",
    )
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")
    memory_regime: Optional[str] = Field(
        None,
        description=("Optional memory isolation for this goal's runs: 'shared', 'scoped' "
                     "(reads shared memory, its own findings stay quarantined until it "
                     "completes verified) or 'sealed' (sees only its own findings). Omit "
                     "for the deployment default. Never weaker than the creating run's."))
    report_back: bool = Field(
        False,
        description=("056 WS7: when true the FULL result re-enters the session that created "
                     "this goal; default false = a one-line 'goal <id> done — <first line>' "
                     "notice (the artefacts are on disk either way)."))
    acceptance_checks: Optional[List[Any]] = Field(
        None,
        description=("Optional TYPED checks the framework executes at run end (fail-closed "
                     "when present). ONLY these types exist — do NOT invent others: "
                     "[{'type':'artifact_glob','pattern':'*.md'}, "
                     "{'type':'http_ok','url':'https://…'}, "
                     "{'type':'artifact','name':'report.md','contains':['A']}, "
                     "{'type':'file_contains','path':'report.md','contains':['A','B'],"
                     "'mode':'all'}]. Prefer setting one when the outcome is mechanically "
                     "checkable. At most 10 checks; malformed checks are rejected, never dropped. "
                     "Paths are run-workspace-relative; HTTP probes require public endpoints. "
                     "file_contains is a case-insensitive literal-substring match — use it "
                     "only for known-exact strings (an id, a file path, a specific number). "
                     "Do NOT use it to assert a report 'discusses'/'covers' a topic (e.g. "
                     "contains=['PnL'] to check the report mentions profit/loss) — a "
                     "semantically-complete report using different wording will fail this "
                     "check and force a wasted retry. For that kind of completeness, describe "
                     "it in plain-English `acceptance` instead and let the completion judge "
                     "read it."),
    )


class GoalAskAction(BaseModel):
    """A durable OWNER-FACING need: "I require a decision/resource from you to proceed"."""
    what: str = Field(..., min_length=8, max_length=600, description=(
        "The decision or resource you need from the owner, as ONE question they can answer in a "
        "word or two (name the options: 'A) … or B) …'). This lands on the durable board, shows in "
        "/status, /pending and the daily digest, and is deduplicated: a matching OPEN ask "
        "is refreshed, never duplicated — so raise it EVERY time you skip on it; never fall back to "
        "'I asked once, I will not spam'."))
    why: str = Field("", max_length=1200, description=(
        "Evidence: what is blocked, since when, what you tried, the numbers. Written for a phone."))
    blocks_goal_ids: Optional[List[str]] = Field(None, description=(
        "Goal ids this ask blocks (they are HELD until the owner answers, then re-armed)."))


class GoalListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(
        None,
        description="Filter: triage/waiting/ready/running/blocked/done/cancelled. Omitted = "
                    "LIVE goals only (triage/waiting/ready/running/blocked); pass 'done' or "
                    "'cancelled' to see history.")
    limit: int = Field(30, ge=1, le=200, description="Newest N rows to show (default 30).")


#: What ``goal_list`` shows when no status filter is given: work that is still
#: on the board. ``done``/``cancelled`` are history and dwarf the live rows
#: within days (357 done vs 1 ready on prod, 2026-08-29).
from core.goal_vocab import LIVE_STATUS_ORDER as GOAL_LIST_LIVE_STATUSES  # noqa: E402


class GoalShowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id.")


class GoalCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id to stop.")
    forever: bool = Field(False, description=(
        "True ONLY when the owner said never again: the title is switched off, so "
        "no stream, planner or agent creates it again (until the owner's /goal "
        "allow). Default: stop just this goal."))


class GoalUnblockAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The blocked goal id.")
    rationale: str = Field("", description="Why it can proceed now (what changed).")


class GoalUpdateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str
    title: Optional[str] = Field(None, min_length=3)
    body: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=10)
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")
    tools: Optional[List[str]] = None


def _autonomy_refusal(execution_context) -> Optional[ActionResult]:
    from core.autonomy_marker import is_autonomous
    sid = getattr(execution_context, "session_id", None)
    psid = getattr(execution_context, "parent_session_id", None)
    if is_autonomous(sid) or is_autonomous(psid):
        return ActionResult(
            error=("Refused: goal mutation is not allowed from an autonomous "
                   "(goal/cron-spawned) session — owner-only. You may goal_create new "
                   "goals or read state."),
            include_in_memory=True)
    if not owner_seat_turn(execution_context):
        return ActionResult(error="Refused: goal mutation requires a genuine owner turn; a leaf or forged turn cannot change standing work.", include_in_memory=True)
    return None


class GoalTool(BaseTool):
    """Agent tool exposing create/list/show/cancel over the durable goal board."""

    def _resolve_board(self) -> GoalBoard:
        if getattr(self, "_goal_board", None) is None:
            from core.runtime_paths import goals_db_path
            data_dir = getattr(self.config, "data_dir", None) if getattr(self, "config", None) else None
            self._goal_board = GoalBoard(goals_db_path(data_dir))
        return self._goal_board

    @staticmethod
    def _user(execution_context) -> str:
        uid = getattr(execution_context, "user_id", None)
        if uid:
            return uid
        from core.identity import resolve_identity
        return resolve_identity()  # owner principal or "local" — never the anon sentinel (ME-D4)

    @staticmethod
    def _creating_turn_is_forged(execution_context) -> bool:
        """True unless this is a genuine owner turn (fail-closed on any probe error)."""
        try:
            from tools.controller.turn_origin import _is_forged_or_autonomous_turn
            return bool(_is_forged_or_autonomous_turn(execution_context, None))
        except Exception:
            return True

    @BaseTool.action("Record a DURABLE goal pursued across sessions (beyond this turn). "
                     "Use for ongoing work; use `task` for this-turn TODOs and `cronjob` "
                     "for time-scheduled runs.", param_model=GoalCreateAction)
    async def goal_create(self, params: GoalCreateAction, execution_context=None) -> ActionResult:
        # A leaf / sub-agent reports to its parent; it must not start durable
        # autonomous work of its own (cronjob is delegate_blocked, goal_ask
        # refuses a leaf — the same rule).
        if _is_leaf_context(execution_context):
            return ActionResult(error="Refused: a leaf/sub-agent cannot create a durable goal — "
                                      "return the proposed goal to your parent instead.",
                                include_in_memory=True)
        if _SELF_REFERENTIAL_ACCEPTANCE.search(params.acceptance or ""):
            # 2026-10-17: 5 "record row" goals in 11 min — each judged FAIL,
            # each breeding an Unblock ask. A goal cannot prove itself.
            return ActionResult(
                error="Refused: this goal's acceptance is the goal board itself (a goal row / "
                      "goal_show read-back), so no run can ever complete it. Put a record in a "
                      "doctrine file or skill (filesystem / skill_manage), or schedule a job, and "
                      "give a goal an acceptance with an effect outside the board.",
                include_in_memory=True)
        user_id = self._user(execution_context)
        payload: dict = {}
        # 031: the AUDIT stamp — who created this goal — set on EVERY turn kind,
        # so the planner's own outcome accounting can count the goals it queued.
        # Distinct from origin_session_id (below), which is the WAKE target and
        # is stamped only for a genuine owner turn.
        _sid = getattr(execution_context, "session_id", None)
        if _sid:
            payload["created_by_session_id"] = str(_sid)
        # The creating session is the only session with a stake in this goal's
        # completion; the dispatcher's self-wake re-entry targets it (and ONLY it —
        # see GoalDispatcher._self_wake). Only a GENUINE turn (the owner's chat
        # session) is stamped: a planner/goal/cron session or a forged re-entry is
        # itself finished by the time the goal completes, and waking it would just
        # be the completion echo again — a planner session woken this way could
        # even queue more goals. Fail-closed: an undecidable origin stamps nothing.
        _origin = getattr(execution_context, "session_id", None)
        if _origin and not self._creating_turn_is_forged(execution_context):
            payload["origin_session_id"] = str(_origin)
        owner_turn = owner_seat_turn(execution_context)
        allowed: List[str] = []
        if params.tools:
            _allowed_set = owner_turn_goal_tools() if owner_turn else allowed_self_goal_tools()
            allowed = [t for t in params.tools if t in _allowed_set]
            dropped = [t for t in params.tools if t not in _allowed_set]
            if dropped:
                logger.info("goal_create: dropped non-allowlisted tools %s (kept %s)", dropped, allowed)
        inferred: set = set()
        if not allowed:
            # S4 (dynamic tool rig, 2026-07-20): under progressive tool disclosure an
            # inference-only goal stays TOOLS-LESS — a written payload.tools would
            # short-circuit dispatch's wide autonomous default (the create-time
            # keyword guess was the last narrowing site), and the S1 catalog +
            # load_tool cover anything the guess would have added. Dispatch-time
            # inference (dispatcher._resolve_goal_tools) remains as a WIDENING hint.
            # Flag off => legacy Proposal-009 inference, byte-identical.
            _disclosure_on = False
            try:
                from core.config_policy import tool_progressive_disclosure
                _disclosure_on = tool_progressive_disclosure()
            except Exception:
                _disclosure_on = False
            if not _disclosure_on:
                # Proposal 009 option B: no (valid) explicit tools — infer from the goal's own text.
                inferred = _infer_tools_from_text(params.title, params.body, params.acceptance)
                if inferred:
                    logger.info("goal_create: inferred tools %s from goal text", sorted(inferred))
        if allowed or inferred:
            payload["tools"] = sorted(set(allowed) | inferred | set(_SELF_GOAL_BASELINE_TOOLS))
        if params.rig:
            # 057 WS-A. Validated HERE so an unknown name is refused with the
            # valid list rather than stored and silently ignored at dispatch.
            # Stored even when `tools` is also set: `tools` wins at dispatch
            # (resolve_rig_tools), and dropping the rig would lose the author's
            # stated intent for a later edit.
            from core.config_policy.rigs import is_rig, rig_names
            if not is_rig(params.rig):
                return ActionResult(
                    error=f"Unknown tool rig '{params.rig}'. Valid rigs: "
                          f"{', '.join(rig_names())}.",
                    include_in_memory=True)
            refusal = None if owner_turn else agent_rig_refusal(params.rig)
            if refusal:
                return ActionResult(error=refusal, include_in_memory=True)
            payload["rig"] = params.rig.strip().lower()
        # 060 WS-5: the goal pins its doctrine (seeded every run; no tool granted).
        from core.config_policy.rigs import pinned_skills
        if pinned_skills({"skills": params.skills}):
            payload["skills"] = pinned_skills({"skills": params.skills})
            # 081 WS-5.4: the one pin validator the cron lane gate runs.
            from core.cron_lanes import LaneStopped, check_pinned_skills
            try:
                check_pinned_skills(payload, user_id=self._user(execution_context))
            except LaneStopped as exc:
                return ActionResult(error=f"Cannot create goal: {exc}", include_in_memory=True)
        # 068 B4: a goal created by a target-bound run inherits the target.
        from core.wallet.buy_target import PAYLOAD_KEY as _TARGET_KEY, inherit_target
        try:
            _target = inherit_target(execution_context, params.target_token)
        except ValueError as exc:
            return ActionResult(error=f"Cannot create goal: invalid target_token: {exc}",
                                include_in_memory=True)
        if _target:
            payload[_TARGET_KEY] = _target
            # DEFI-5: owner-typed = trust; an address the owner did not type is restrict-only.
            from core.wallet.buy_target import TARGET_AUTHOR_KEY, target_provenance
            _tp = target_provenance(execution_context, _target, owner_turn)
            if _tp:
                payload[TARGET_AUTHOR_KEY] = _tp
        # 085 W3 (D3): an owner turn's standing MONEY goal waits for the owner's tap.
        _money = money_job_reasons(tools=payload.get("tools"), rig=payload.get("rig"),
                                   target=_target) if owner_turn else []
        if _money:
            return await self._money_goal_card(params, payload, _target, _money,
                                               execution_context)
        board = self._resolve_board()
        if params.acceptance:
            payload["acceptance"] = params.acceptance
        if params.report_back:
            payload["report_back"] = True
        if params.memory_regime is not None or execution_context is not None:
            from modules.memory.scope import goal_create_regime  # 025: clamped, never a label
            _regime, _err = goal_create_regime(
                params.memory_regime, getattr(execution_context, "session_id", None))
            if _err:
                return ActionResult(error=f"Cannot create goal: {_err}", include_in_memory=True)
            if _regime:
                payload["memory_regime"] = _regime
        if params.acceptance_checks is not None:
            from agents.task.runtime.acceptance_checks import validate_checks
            try:
                payload["acceptance_checks"] = validate_checks(params.acceptance_checks)
            except (ValueError, TypeError) as exc:
                return ActionResult(error=f"Cannot create goal: invalid acceptance checks: {exc}",
                                    include_in_memory=True)
        try:
            goal = board.create(
                user_id=user_id, title=params.title, body=params.body, priority=params.priority,
                payload=payload or None,
                depends_on=params.depends_on,
                # 036 §3.3: the ONE predicate. The allowlist filter above and the
                # board's grant check read the same ceiling, so they cannot drift.
                actor="owner_seat" if owner_turn else "agent",
                tool_ceiling=sorted(allowed_self_goal_tools()
                                    | set(_SELF_GOAL_BASELINE_TOOLS)),
            )
        except DuplicateGoalError as e:
            return ActionResult(
                error=(f"Duplicate: near-identical to goal `{e.match_id}` '{e.match_title}' "
                       f"(similarity {e.similarity:.2f}). Extend that goal instead, or change scope."),
                include_in_memory=True)
        except ValueError as e:
            # T2.1 Task 4: board.create validates depends_on BEFORE the row is
            # written (all-or-nothing), so a bad dep (unknown id / cross-tenant /
            # wrong kind) never leaves an orphan goal row — this surfaces as a
            # plain error result, same as any other create-time ValueError.
            return ActionResult(error=f"Cannot create goal: {e}", include_in_memory=True)
        tool_note = f" tools={payload['tools']}" if payload.get("tools") else ""
        dep_note = f" depends_on={board.dependencies(goal.id)}" if params.depends_on else ""
        drop_note = _dropped_tools_note(params.tools)
        mismatch_warning = _compute_tool_mismatch_warning(payload.get("tools"), params.acceptance_checks)
        warn_note = f"\n{mismatch_warning}" if mismatch_warning else ""
        recurring = _recurring_work_warning(params.title, params.body,
                                            str(payload.get("acceptance") or ""))
        if recurring:
            warn_note += f"\n{recurring}"
        return ActionResult(extracted_content=f"Created goal `{goal.id}` (status={goal.status}){tool_note}{dep_note}: {goal.title}{drop_note}{warn_note}",
                            include_in_memory=True)

    async def _money_goal_card(self, params, payload: dict, target, reasons: List[str],
                               execution_context) -> ActionResult:
        """085 W3: the ``/goal new …`` line for this goal, as the owner's card."""
        text = params.title.strip()
        if (params.body or "").strip():
            text += ": " + params.body.strip()
        if (params.acceptance or "").strip():
            text += " Done when: " + params.acceptance.strip()
        grant = grant_option_words(tools=payload.get("tools"), rig=payload.get("rig"),
                                   target=target, skills=payload.get("skills"))
        try:
            words = ["/goal", "new"] + prose_words(text) + grant
        except ValueError as exc:
            return ActionResult(error=f"Nothing was written: {exc}.", include_in_memory=True)
        skipped = [name for name, value in (
            ("priority", params.priority != 5), ("depends_on", params.depends_on),
            ("memory_regime", params.memory_regime), ("report_back", params.report_back),
            ("acceptance_checks", params.acceptance_checks)) if value]
        return await raise_job_card(
            self, execution_context, words, what="goal", reasons=reasons,
            details=[f"Goal: {params.title.strip()}"] + [f"Grant: {w}" for w in grant],
            not_carried=skipped,
            later="find the goal with goal_list and set those fields with goal_update.")

    @BaseTool.action("Raise a DURABLE owner ask that HOLDS specific goals (blocks_goal_ids) until "
                     "the owner answers. For any other owner decision use owner_ask — it tells the "
                     "owner, offers taps and hands the answer to this rail's next run. Lands on the "
                     "board, shows in /status, /pending and the daily digest; dedups against your "
                     "OPEN asks, so call it every time you skip on the same blocker.",
                     param_model=GoalAskAction)
    async def goal_ask(self, params: GoalAskAction, execution_context=None) -> ActionResult:
        # A leaf / sub-agent reports to its parent, not to the owner: refuse.
        if getattr(execution_context, "is_sub_agent", False) or \
                str(getattr(execution_context, "role", "") or "") == "leaf":
            return ActionResult(error="Refused: a leaf/sub-agent cannot raise an owner ask — "
                                      "return the blocker to your parent instead.",
                                include_in_memory=True)
        user_id = self._user(execution_context)
        sid = str(getattr(execution_context, "session_id", "") or "")
        from core.surfaces.owner_address import is_owner_tenant
        from core.autonomy_marker import goal_for_session
        if not is_owner_tenant(user_id):
            return ActionResult(error="Refused: this tenant cannot raise an owner ask.")
        own_goal = goal_for_session(sid)
        if params.blocks_goal_ids and not owner_seat_turn(execution_context):
            if any(goal_id != own_goal for goal_id in params.blocks_goal_ids):
                return ActionResult(error="Refused: a run may hold only its own goal for an owner decision.")
        board = self._resolve_board()
        before = {a.id for a in board.asks(user_id=user_id, status="open")}
        # Same provenance and tap options owner_ask stamps, so the answer reaches
        # the asking rail's next run and the owner can answer with one tap.
        extra = {"origin": "agent", "session_id": sid}
        try:
            from tools.controller.owner_ask_action import _rail, ask_options
            rail_id, rail_kind = _rail(sid)
            if rail_id:
                extra.update(rail_id=rail_id, rail_kind=rail_kind)
            options = ask_options(params.what)
            if options:
                extra["options"] = options
        except Exception:
            pass
        ask = board.create_ask(
            user_id=user_id, what=params.what.strip(), why=(params.why or "").strip(),
            blocks_goal_ids=list(params.blocks_goal_ids or []),
            extra_payload=extra)
        if ask.id in before:
            import datetime as _dt
            since = _dt.datetime.fromtimestamp(float(ask.created_at or 0), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return ActionResult(
                extracted_content=(f"Ask `{ask.id}` is ALREADY OPEN since {since} (refreshed, not duplicated — "
                                   f"the store dedups, no need to re-ask in chat). The owner sees it in /status, "
                                   f"/pending and the daily digest; until it is answered, keep applying the "
                                   f"conservative reading and log the skip against ask {ask.id}."),
                include_in_memory=True)
        return ActionResult(
            extracted_content=(f"Raised owner ask `{ask.id}` (open): {ask.title}. It stays visible on every owner "
                               f"seat until answered; re-calling goal_ask with the same question refreshes it. "
                               f"Log skips against ask {ask.id} and do NOT also message the owner about it."),
            include_in_memory=True)

    @BaseTool.action("List your durable goals, newest first — LIVE ones by default "
                     "(pass status='done'/'cancelled' for history).",
                     param_model=GoalListAction)
    async def goal_list(self, params: GoalListAction, execution_context=None) -> ActionResult:
        # A VIEW must read newest-first over the tenant's rows. ``board.list`` is the
        # dispatcher's ``priority DESC, created_at ASC LIMIT`` order: used here it
        # showed the OLDEST 100 rows of a 409-row prod board and zero manifest stream
        # legs (priority 2/3 sort after every priority-5 row), so the agent told the
        # owner it had no trading goals while ten clean cycles had run (2026-08-29).
        board = self._resolve_board()
        user_id = self._user(execution_context)
        statuses = (params.status,) if params.status else GOAL_LIST_LIVE_STATUSES
        goals = board.list_recent(user_id=user_id, statuses=statuses, limit=params.limit)
        counts = board.status_counts(user_id=user_id)
        totals = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "0 rows"
        if not goals:
            what = f"status={params.status}" if params.status else "live"
            return ActionResult(
                extracted_content=f"No {what} goals. Board totals: {totals}.",
                include_in_memory=True)
        head = (f"Durable goals (newest first, showing {len(goals)} "
                f"{'status=' + params.status if params.status else 'live'}; "
                f"board totals: {totals}):")
        lines = [f"- `{g.id}` [{g.status}] p{g.priority}: {g.title}" for g in goals]
        return ActionResult(extracted_content=head + "\n" + "\n".join(lines),
                            include_in_memory=True)

    @BaseTool.action("Show one goal's detail + recent events.", param_model=GoalShowAction)
    async def goal_show(self, params: GoalShowAction, execution_context=None) -> ActionResult:
        board = self._resolve_board()
        user_id = self._user(execution_context)
        g = board.get(params.goal_id, user_id=user_id)
        if not g:
            return ActionResult(error="Goal not found.", include_in_memory=True)
        payload = g.payload or {}
        lines = [
            f"Goal `{g.id}` [{g.status}] p{g.priority}",
            f"Title: {g.title}",
            f"Failures: {g.consecutive_failures}/{g.max_retries}",
            f"Result: {(g.result or '')[:500]}",
        ]
        # §5.2/§5.3 stewardship: the agent sees its goal's full contract + attempt
        # history so it can maintain its pipeline and its user's picture of it.
        if payload.get("acceptance"):
            lines.append(f"Acceptance: {str(payload['acceptance'])[:300]}")
        if payload.get("acceptance_checks"):
            lines.append(f"Typed checks: {payload['acceptance_checks']}")
        if payload.get("outcome"):
            lines.append(f"Outcome: {str(payload['outcome'])[:300]}")
        if g.last_failure_error:
            lines.append(f"Last failure: {str(g.last_failure_error)[:300]}")
        attempts = payload.get("attempts") or []
        if attempts:
            lines.append("Attempts:")
            for a in attempts[-5:]:
                if isinstance(a, dict):
                    lines.append(f"  - {str(a.get('error') or '')[:200]}")
        # T2.1 Task 4: surface DAG edges (when any exist) — what this goal is
        # still waiting on, and what it in turn blocks. Capped at 10 ids (review
        # Minor; mirrors the attempts[-5:] compact-tail precedent above) so a
        # goal with a wide fan-out never blows the message out.
        def _id_title_list(ids: List[str]) -> str:
            capped = ids[:10]
            parts = []
            for dep_id in capped:
                dep_goal = board.get(dep_id, user_id=user_id)  # a foreign id shows as '?', never its title
                parts.append(f"{dep_id} ({dep_goal.title if dep_goal else '?'})")
            text = ", ".join(parts)
            if len(ids) > len(capped):
                text += f" (+{len(ids) - len(capped)} more)"
            return text

        deps = board.dependencies(g.id)
        if deps:
            lines.append("waiting on: " + _id_title_list(deps))
        blocks = board.dependents(g.id)
        if blocks:
            lines.append("blocks: " + _id_title_list(blocks))
        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

    @BaseTool.action("Requeue a BLOCKED goal with a rationale (§5.3 stewardship; "
                     "resets its retry budget).", param_model=GoalUnblockAction)
    async def goal_unblock(self, params: GoalUnblockAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        board = self._resolve_board()
        ok = board.unblock(params.goal_id, user_id=self._user(execution_context),
                           rationale=params.rationale)
        if not ok:
            return ActionResult(error=f"Cannot unblock `{params.goal_id}`: not a blocked goal of yours.",
                                include_in_memory=True)
        return ActionResult(
            extracted_content=f"Unblocked goal `{params.goal_id}` (retry budget reset).",
            include_in_memory=True)

    @BaseTool.action("Stop a durable goal: once by default; forever=true only when the "
                     "owner said never again (the same as the owner's /goal stop).",
                     param_model=GoalCancelAction)
    async def goal_cancel(self, params: GoalCancelAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        board = self._resolve_board()
        user_id = self._user(execution_context)
        # 080 D4: "never again" is the OWNER's word — the switch-off is written
        # only in a genuine owner turn, never by an autonomous run on its own.
        if params.forever and not owner_seat_turn(execution_context):
            return ActionResult(error="Refused: only the owner can switch a goal off for "
                                      "good — stop it once, or ask the owner to run "
                                      "/goal stop <id> --forever.", include_in_memory=True)
        goal = board.get(params.goal_id, user_id=user_id)
        if goal is None or getattr(goal, "kind", "goal") != "goal":
            return ActionResult(extracted_content="Nothing to stop.", include_in_memory=True)
        from core.goal_suppressions import stop_goal
        res = stop_goal(board, user_id=user_id, goal=goal, forever=params.forever)
        if not res.ok:
            return ActionResult(error=res.message, include_in_memory=True)
        return ActionResult(extracted_content=res.message, include_in_memory=True)

    @BaseTool.action("Edit a goal's title/body/priority/acceptance/tools (owner-only).",
                     param_model=GoalUpdateAction)
    async def goal_update(self, params: GoalUpdateAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        board = self._resolve_board()
        user_id = self._user(execution_context)
        row = board.get(params.goal_id, user_id=user_id)
        if row is not None:
            # DATA-7: an ask is decided (owner_ask / a tap) — never rewritten
            # through goal_update.
            if getattr(row, "kind", "goal") == "ask":
                return ActionResult(error=("Refused: goal_update does not edit an ask. "
                                           "Asks are decided with owner_ask or a tap."),
                                    include_in_memory=True)
        owner_turn = owner_seat_turn(execution_context)
        patch: dict = {}
        if params.acceptance is not None:
            patch["acceptance"] = params.acceptance
        if params.tools is not None:
            _ceiling = owner_turn_goal_tools() if owner_turn else allowed_self_goal_tools()
            patch["tools"] = [t for t in params.tools if t in _ceiling]
            # 085 W3 (D3): a money grant on an owner turn waits for the owner's tap;
            # the other fields of this call do not wait for it.
            _money = money_job_reasons(tools=patch["tools"]) if owner_turn else []
            if _money and row is not None:
                tools_now = list(patch.pop("tools"))
                card = await raise_job_card(
                    self, execution_context,
                    ["/goal", "grant", row.id] + grant_option_words(tools=tools_now),
                    what="goal tool grant", reasons=_money,
                    details=[f"Goal `{row.id}`: {row.title}",
                             f"Tools: {', '.join(tools_now)}"],
                    not_carried=[], later="")
                if not (patch or params.title or params.body or params.priority):
                    return card
                ok = board.update_fields(
                    params.goal_id, user_id=user_id, title=params.title, body=params.body,
                    priority=params.priority, payload_patch=patch or None)
                done = (f"Updated goal `{params.goal_id}` (the other fields)." if ok
                        else "The other fields: goal not found / terminal / nothing to change.")
                if card.error:
                    return ActionResult(error=f"{done} {card.error}", include_in_memory=True)
                return ActionResult(extracted_content=f"{done} {card.extracted_content}",
                                    include_in_memory=True)
            if owner_turn:
                # 085 W8 A4: the tool set is replaced whole, so its writer is this
                # turn — an owner turn's non-money grant (web_fetch, email, …) on an
                # agent-written goal must not be dropped by the dispatch re-check.
                patch["actor"] = "owner_seat"
        ok = board.update_fields(
            params.goal_id, user_id=user_id,
            title=params.title, body=params.body, priority=params.priority,
            payload_patch=patch or None)
        return ActionResult(
            extracted_content=(f"Updated goal `{params.goal_id}`." if ok
                               else "Goal not found / terminal / nothing to change."),
            include_in_memory=True)


def goals_enabled() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.goals_enabled()


def register_goal_tool(force: bool = False) -> bool:
    """Register the 'goal' descriptor + class IFF GOALS_ENABLED (or forced).

    Delegates to ``register_optional_tool`` (single shared factory). No-op when goals
    are off, so default deploys are unaffected. ``goal`` is never in the default
    ``tool_ids`` — agents opt in.
    """
    from tools.descriptors import (
        ToolDescriptor,
        ToolCategory,
        register_optional_tool,
    )

    return register_optional_tool(
        "goal",
        GoalTool,
        ToolDescriptor(
            name="goal",
            description=("Record/list/update/cancel durable cross-session goals "
                        "(goal_create/list/show/update/cancel)"),
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=80,
            is_optional=True,
        ),
        goals_enabled,
        force=force,
    )
