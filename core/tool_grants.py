"""Who may put a tool on a goal — ONE predicate (036 §3.3).

``payload.tools`` is consumed VERBATIM at dispatch (``dispatcher._resolve_tools``)
and at the cron read (``cron/runner.py::resolve_cron_tools``). Until 036 every
production writer decided for itself whether that was allowed. This module is the
one place the decision lives:

* :func:`assert_grantable` — called by ``GoalBoard.create`` (the choke point every
  writer passes through) and by the cron payload read. It refuses a GATED tool id
  (``money`` | ``high_impact`` | ``exec``, derived from
  ``core/tool_capabilities.py`` — never a hand list, so a new money tool is
  covered on the day it is registered) unless the actor may carry it:

  ================  =========================================================
  actor             may carry a gated id when …
  ================  =========================================================
  ``owner_seat``    always — the owner asking from an authenticated seat IS the
                    operator grant (``/trade``, ``polyrob goals create
                    --tools``, a genuine owner chat turn).
  ``agent``         the id is inside the caller's ``ceiling``
                    (``allowed_self_goal_tools()``) — the agent never
                    self-grants past it.
  ``none``          never — a writer that cannot carry tools (asks).
  ================  =========================================================

  An UNGATED id always passes. ``actor=None`` is the legacy/test path and passes
  unchanged; ``tests/test_board_create_grant_ratchet.py`` pins that every
  production ``board.create(`` call site names its actor, which is what turns the
  convention into a boundary. Recurring work is a cron job; its tools are the
  job's own (``payload.tools`` / ``payload.rig``), judged by the same predicate.

The self-goal ceiling (:func:`allowed_self_goal_tools`), its baseline and the
text inference live here too (U5/U7). Pure core: ``core.tool_capabilities`` and
``core.config_policy`` only.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

import logging

from core.config_policy.profiles import profile as _profile
from core.tool_capabilities import ids_with

logger = logging.getLogger(__name__)

ACTOR_OWNER = "owner_seat"
ACTOR_AGENT = "agent"
ACTOR_NONE = "none"
ACTORS = (ACTOR_OWNER, ACTOR_AGENT, ACTOR_NONE)


class GrantRefused(ValueError):
    """A writer tried to put a gated tool on a goal it may not grant."""

    def __init__(self, tools: Sequence[str], actor: Optional[str], why: str):
        self.tools = list(tools)
        self.actor = actor
        self.why = why
        super().__init__(
            f"{', '.join(self.tools)} NOT granted ({why}). Only the owner grants a "
            f"money, host or high-impact tool — from an owner seat.")


def gated_ids() -> frozenset:
    """The ids a writer may not put on a goal without authority."""
    return ids_with("money") | ids_with("high_impact") | ids_with("exec")


def split_grantable(tools: Iterable[str], *, actor: Optional[str],
                    ceiling: Optional[Iterable[str]] = None):
    """``(kept, refused, why)`` — the pure half of :func:`assert_grantable`."""
    ids = [str(t) for t in (tools or []) if str(t or "").strip()]
    if actor is None or actor == ACTOR_OWNER:
        return ids, [], ""
    gated = gated_ids()
    allowed = set(ceiling or ())
    refused: List[str] = []
    why = ""
    for t in ids:
        if t not in gated:
            continue
        if actor == ACTOR_AGENT:
            if t not in allowed:
                refused.append(t)
                why = "outside what agent-written work may hold"
            continue
        refused.append(t)
        why = "this writer cannot carry tools"
    return [t for t in ids if t not in refused], refused, why


def assert_grantable(tools: Optional[Iterable[str]], *, actor: Optional[str],
                     ceiling: Optional[Iterable[str]] = None) -> List[str]:
    """Return *tools* unchanged, or raise :class:`GrantRefused`. See the module
    docstring for the actor table."""
    if actor is not None and actor not in ACTORS:
        raise GrantRefused(list(tools or []), actor, f"unknown actor {actor!r}")
    kept, refused, why = split_grantable(tools or [], actor=actor, ceiling=ceiling)
    if refused:
        raise GrantRefused(refused, actor, why)
    return kept


#: ``payload`` key a writer stamps (``GoalBoard.create``, the cron tool, the owner
#: seat's ``create_cron``): WHO wrote the row (085 W8 A4).
ACTOR_KEY = "actor"


def dispatch_recheck(tools: Optional[Iterable[str]], payload: Optional[Mapping[str, Any]], *,
                     declared: Optional[Iterable[str]],
                     agent_ceiling: Optional[Iterable[str]] = None) -> Tuple[List[str], List[str]]:
    """085 W8 A4: re-judge a row's DECLARED tool set at dispatch, keyed on the
    stored :data:`ACTOR_KEY`. Returns ``(tools to run with, refused ids)``.

    *declared* is what the row itself asks for (``payload.tools``, else its rig's
    ids); a gated id in it that the stored actor may not hold is dropped from
    *tools*. A row with no stored actor (written before the stamp) or no declared
    set passes unchanged; an unknown actor value holds no gated id (fail closed).
    The creation check is the same predicate; this catches a row written past the
    tool layer (a planted or legacy row) and a rig the creation check never judged."""
    ids = [str(t) for t in (tools or [])]
    data = payload if isinstance(payload, Mapping) else {}
    wanted = [str(t) for t in (declared or [])]
    if ACTOR_KEY not in data or not wanted:
        return ids, []
    actor = data.get(ACTOR_KEY)
    actor = actor if actor in ACTORS else ACTOR_NONE
    _kept, refused, _why = split_grantable(wanted, actor=actor, ceiling=agent_ceiling)
    return [t for t in ids if t not in refused], refused


# --- The self-goal grant (U5, U7: moved from tools/goal_tools.py) ---------------

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
SELF_GOAL_ALLOWED_TOOLS = frozenset(_profile("ceiling:self_goal"))


def allowed_self_goal_tools() -> frozenset:
    """Tool ids an agent-created goal may request. Under effective autonomous mode
    (AUTONOMY_MODE=autonomous on a single-owner instance) this expands to the full
    AUTONOMOUS_MODE_TOOLS grant; money-spend/host tools are in NEITHER set."""
    try:
        from core.config_policy.autonomous_tools import autonomous_mode_tools
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
            return frozenset(SELF_GOAL_ALLOWED_TOOLS | set(autonomous_mode_tools()))
    except Exception as e:
        # A-9: the fallback is the narrow ceiling (safe), but it is never silent.
        logger.warning("self-goal tool ceiling: the autonomy probe failed (%s); "
                       "using the supervised ceiling", e)
    return SELF_GOAL_ALLOWED_TOOLS


# Proposal 009 #1 (2026-07-14): whenever a self-created goal carries ANY tools payload, union in
# this safe baseline so the dispatched session is never starved of basics (night-1 failure mode:
# tools=['twitter'] sessions had no filesystem; text-only goals had no web_fetch to research).
SELF_GOAL_BASELINE_TOOLS = _profile("baseline:self_goal")

# Proposal 009 option B: when the agent sets no `tools`, infer them from the goal's own text —
# the night-1 blocked goals literally named their needed tool in title/acceptance ("Publish
# queued OSS launch X thread" → twitter) but dispatched tool-starved. Substring match on
# lowercased title+body+acceptance; result is still filtered through allowed_self_goal_tools(),
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


def infer_tools_from_text(*texts: Optional[str]) -> set:
    """The tool ids a goal's own text names, inside the self-goal ceiling."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return set()
    found = {tool for tool, tokens in _TOOL_TEXT_TOKENS.items()
             if any(tok in blob for tok in tokens)}
    return found & allowed_self_goal_tools()


__all__ = [
    "ACTORS", "ACTOR_AGENT", "ACTOR_KEY", "ACTOR_NONE", "ACTOR_OWNER", "GrantRefused",
    "SELF_GOAL_ALLOWED_TOOLS", "SELF_GOAL_BASELINE_TOOLS", "allowed_self_goal_tools",
    "assert_grantable", "dispatch_recheck", "gated_ids", "infer_tools_from_text",
    "split_grantable",
]
