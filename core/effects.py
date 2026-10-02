"""ONE effect classification for every external write (proposal 033).

Two layers. The tool's ``writes_*`` capability row (``core/tool_capabilities.py``)
is the mandatory CEILING; the action name refines it. Neither alone works: a
per-action scheme cannot classify an MCP tool (every discovered tool collapses to
tool_id ``mcp``), and a per-tool scheme cannot tell ``twitter_search`` from
``twitter_post``.

POLARITY — an unlisted verb on a write-capable tool is assumed to WRITE.
``NON_WRITE_ACTIONS`` lists the verbs that are NOT external writes; everything
else inherits the ceiling. A new WRITE verb is therefore covered the day it lands
(``confidence="tool"``), and a new READ verb over-reports until it is listed —
loud, harmless and self-correcting. The inverse polarity is the bug 033 removes:
``twitter_reply``/``twitter_quote`` were uncooled, unrecorded public posts.

Three seams reach :func:`record_external_write`, and only these three:

- the Controller post-hook (``tools/controller/effect_hooks.py``) — every
  agent-driven action, success and failure;
- the two ``MessageRouter`` methods (``publish`` and ``send_message_ex``) —
  outbound sends that never came from an action;
- a direct call from a writer with neither (cron delivery, the app supervisor,
  the settlement notifier).

Tier-0 by necessity: ``core/app_service/supervisor.py`` may not import
``agents.*`` or ``tools.*`` and ``modules/x402/*`` may not import ``tools.*``.
Turn origin is resolved by the caller and handed in as data.

Fail modes: the recorder fails OPEN (telemetry never breaks what it observes);
the pause decision helper is used by a gate that registers fail-CLOSED.
"""
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional

from core.env import bool_env
from core.lazy_views import lazy_module_getattr, view
from core.tool_capabilities import CATALOG_ALIASES, TOOL_CAPABILITIES
from core.verb_policy import EFFECT_CLASSES, VERB_POLICY, ordered_ids_where

logger = logging.getLogger(__name__)

#: capability token -> effect class. The ONE mapping between the two vocabularies.
TOKEN_TO_EFFECT: Dict[str, str] = {
    "writes_social": "social",
    "writes_comms": "comms",
    "writes_public": "public",
    "writes_money": "money",
    "writes_code": "code",
    "writes_self": "self",
    "writes_network": "network",
}

#: When a tool carries several ceilings, the most consequential one wins unless an
#: explicit action row says otherwise.
_EFFECT_PRIORITY = ("money", "public", "social", "comms", "self", "code", "network")

# The three row views below are DERIVED from the one per-action table
# (core/verb_policy.py, rows in core/verb_policy_rows.py, 067 P1): classify a verb
# there, by its ``effect`` field. Public names and shapes are unchanged.

# 067 P4 prerequisite: the three views are LAZY (``core/lazy_views.py``) — built
# together on the first read of any of them, after the pack loader's phase 1, by
# the module ``__getattr__`` at the end of this file. Functions here read them with
# ``view(__name__, NAME)``.
#
#   NON_WRITE_ACTIONS: Dict[str, FrozenSet[str]] — verbs on a write-capable tool
#     that are NOT external writes (``effect="none"``). ⚠️ A row here SILENCES a
#     verb — telemetry and the pause gate both skip it — so every entry must be a
#     read or a workspace-local act. The ratchet
#     (tests/test_effect_classification_ratchet.py) pins each verb to a real action.
#   WRITE_ACTIONS: Dict[str, Dict[str, str]] — explicit WRITE rows on a
#     write-capable tool (``confidence="action"``). An action here may name a class
#     other than the tool's top ceiling (a DM is ``comms`` on a ``social`` tool).
#     Verbs on no list still write, at the ceiling, with ``confidence="tool"``.
#   ACTION_EFFECTS: Dict[str, Optional[str]] — actions registered directly on the
#     Controller with NO owning tool_id (``RegisteredAction.tool is None``), so the
#     ceiling cannot resolve for them. ``None`` means "explicitly not an external
#     write". An unlisted tool-less action is not a write either — these rows are
#     the documented decisions.
_ROW_VIEWS: Optional[tuple] = None


def _row_views() -> tuple:
    """``(NON_WRITE_ACTIONS, WRITE_ACTIONS, ACTION_EFFECTS)``, built once. The query
    records this view, so a policy row registered after the first read that would
    belong here is refused rather than silently missing (067 P2)."""
    global _ROW_VIEWS
    if _ROW_VIEWS is None:
        non_write: Dict[str, FrozenSet[str]] = {}
        write: Dict[str, Dict[str, str]] = {}
        action_effects: Dict[str, Optional[str]] = {}
        for name in ordered_ids_where(effect=lambda e: e != "inherit"):
            row = VERB_POLICY[name]
            if row.tool is None:
                action_effects[row.name] = None if row.effect == "none" else row.effect
            elif row.effect == "none":
                non_write[row.tool] = non_write.get(row.tool, frozenset()) | {row.name}
            else:
                write.setdefault(row.tool, {})[row.name] = row.effect
        _ROW_VIEWS = (non_write, write, action_effects)
    return _ROW_VIEWS

#: The classes the v1 pause gate refuses on an autonomous turn. ``code``,
#: ``self`` and ``network`` are observe-only until their rows have been watched
#: in production — so they deliberately have NO pause kind yet: ``allows()``
#: denies an undeclared kind under ANY pause, and a declared kind with no caller
#: is the dormant-kind trap the autonomy ratchet exists to catch.
GATED_EFFECTS: FrozenSet[str] = frozenset({"social", "comms", "public", "money"})

#: gated effect -> the ``core.autonomy_control.allows()`` kind it gates on.
_PAUSE_KIND: Dict[str, str] = {
    "social": "social_post",
    "comms": "outbound_comms",
    "public": "oversight_deploy",
    "money": "spend",
}


@dataclass(frozen=True)
class EffectVerdict:
    effect: str
    #: "action" = an explicit row · "tool" = the ceiling · "hint" = MCP narrowed it
    confidence: str


def _tid(tool_id: Optional[str]) -> str:
    raw = str(tool_id or "").strip().lower()
    return CATALOG_ALIASES.get(raw, raw)


def ceiling(tool_id: Optional[str]) -> Optional[str]:
    """The top effect class a tool is capable of, or None when it writes nothing."""
    caps = TOOL_CAPABILITIES.get(_tid(tool_id), frozenset())
    effects = {TOKEN_TO_EFFECT[c] for c in caps if c in TOKEN_TO_EFFECT}
    for candidate in _EFFECT_PRIORITY:
        if candidate in effects:
            return candidate
    return None


def classify_effect(tool_id: Optional[str], action: str, *,
                    mcp_read_only: bool = False) -> Optional[EffectVerdict]:
    """The effect of *action* (owned by *tool_id*), or None when it writes nothing.

    ``mcp_read_only`` is a server-declared MCP annotation (``readOnlyHint``). It
    may only NARROW: nothing a third-party server declares can raise trust.
    """
    action = str(action or "")
    tid = _tid(tool_id)
    if not tid:
        declared = view(__name__, "ACTION_EFFECTS").get(action)
        return EffectVerdict(declared, "action") if declared else None
    if action in view(__name__, "NON_WRITE_ACTIONS").get(tid, frozenset()):
        return None
    top = ceiling(tid)
    if top is None:
        return None
    if tid == "mcp" and mcp_read_only:
        return None
    explicit = view(__name__, "WRITE_ACTIONS").get(tid, {}).get(action)
    if explicit:
        return EffectVerdict(explicit, "action")
    return EffectVerdict(top, "tool")


def pause_kind_for(effect: str, tool_id: Optional[str] = None) -> Optional[str]:
    """The ``allows()`` kind a GATED effect is judged by, or None (observe-only)."""
    if effect not in GATED_EFFECTS:
        return None
    if effect == "public" and _tid(tool_id) == "app_service":
        return "app_deploy"
    return _PAUSE_KIND[effect]


def effect_pause_refusal(effect: str, tool_id: Optional[str], action: str,
                         data_dir: Optional[str] = None) -> Optional[str]:
    """Refusal text when the owner's pause covers this effect, else None.

    The caller decides turn origin first: an OWNER-initiated turn is never gated.
    Raises on a probe fault — the gate that calls this registers fail-CLOSED, and
    ``allows()`` itself already fails closed on an unreadable pause record.
    """
    kind = pause_kind_for(effect, tool_id)
    if kind is None:
        return None
    from core.autonomy_control import allows
    dec = allows(kind, data_dir)
    if dec.allowed:
        return None
    return (f"'{action}' is an autonomous {effect} write and autonomy is "
            f"{dec.reason}. Resume with `/resume` when you want it back.")


# --- flags -------------------------------------------------------------------------

def external_write_telemetry_enabled() -> bool:
    """``EXTERNAL_WRITE_TELEMETRY`` (default ON): record ``external_write`` rows."""
    return bool_env("EXTERNAL_WRITE_TELEMETRY", True)


def external_write_pause_gate_enabled() -> bool:
    """``EXTERNAL_WRITE_PAUSE_GATE`` (default ON): the effect pre-hook consults
    ``allows()`` for the gated classes on an autonomous turn."""
    return bool_env("EXTERNAL_WRITE_PAUSE_GATE", True)


def external_write_strict() -> bool:
    """``EXTERNAL_WRITE_STRICT`` (default OFF): refuse a write verb that has no
    explicit row (``confidence="tool"``) instead of recording it at the ceiling."""
    return bool_env("EXTERNAL_WRITE_STRICT", False)


# --- the recorder ----------------------------------------------------------------

#: The pinned envelope keys (033 §3.2). A reader may rely on every one of them.
ENVELOPE_KEYS: FrozenSet[str] = frozenset({
    "tool", "action", "target", "surface", "autonomous", "confidence",
    "outcome", "dedup",
})
OUTCOMES: FrozenSet[str] = frozenset({"ok", "error", "denied", "skipped"})


def dedup_key(effect: str, target: str, fingerprint: str) -> str:
    """Short hash of (effect, target, content) — repeat detection, never content."""
    raw = f"{effect}|{target}|{fingerprint}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def record_external_write(*, effect: str, tool: str = "", action: str = "",
                          target: str = "unknown", surface: str = "",
                          autonomous: bool = False, confidence: str = "action",
                          outcome: str = "ok", user_id: str = "",
                          session_id: str = "", fingerprint: str = "",
                          extra: Optional[Dict[str, Any]] = None,
                          event_log: Any = None) -> None:
    """Record ONE outward act as an ``external_write`` row. Never raises.

    An unknown effect class is DROPPED rather than written, so a typo cannot
    pollute the vocabulary every reader depends on. The content itself is never
    stored — only its hash in ``dedup``. ``user_id``/``session_id`` fall back to
    the ambient action-batch identity (``core/exec_identity.py``).
    ``event_log`` is the injected-store seam a writer with its own log handle
    (the app supervisor) passes; default is the process singleton.
    """
    try:
        if effect not in EFFECT_CLASSES:
            logger.debug("record_external_write: unknown effect %r dropped", effect)
            return
        if not external_write_telemetry_enabled():
            return
        from core.event_log import event_log_enabled, get_event_log
        if event_log is None and not event_log_enabled():
            return
        if not user_id or not session_id:
            from core.exec_identity import current_exec_identity
            amb_u, amb_s = current_exec_identity()
            user_id = user_id or amb_u
            session_id = session_id or amb_s
        attrs: Dict[str, Any] = dict(extra or {})
        attrs.update({
            "tool": str(tool or ""),
            "action": str(action or ""),
            "target": str(target or "unknown"),
            "surface": str(surface or ""),
            "autonomous": bool(autonomous),
            "confidence": str(confidence or "action"),
            "outcome": outcome if outcome in OUTCOMES else "ok",
            "dedup": dedup_key(effect, str(target or ""), str(fingerprint or "")),
        })
        from core.event_kinds import EXTERNAL_WRITE
        (event_log if event_log is not None else get_event_log()).record(
            EXTERNAL_WRITE, user_id=str(user_id or ""),
            session_id=str(session_id or ""),
            source=str(surface or tool or "agent"), effect=effect, attrs=attrs)
    except Exception:
        logger.debug("record_external_write failed (fail-open)", exc_info=True)


#: Plain words per effect for the owner-facing rollups (digest, activity feed,
#: insights). One table so the three seats cannot name a class differently.
EFFECT_VERBS: Dict[str, str] = {
    "social": "posted",
    "comms": "messaged",
    "public": "published",
    "money": "moved money",
    "code": "ran code",
    "self": "changed itself",
    "network": "reached out",
}


def effect_line(counts: Dict[str, int]) -> str:
    """``"posted 3 · messaged 5 · published 1"`` — ordered, zero classes omitted."""
    parts = []
    for eff in _EFFECT_PRIORITY:
        n = int(counts.get(eff) or 0)
        if n:
            parts.append(f"{EFFECT_VERBS[eff]} {n}")
    return " · ".join(parts)


def outward_counts(user_id: str, since_ts: float, *, data_dir: Optional[str] = None,
                   autonomous_only: bool = False,
                   outcomes: tuple = ("ok",)) -> Optional[Dict[str, int]]:
    """``{effect: n}`` for *user_id* (plus the untenanted rows a single-owner
    instance writes — the status snapshot's rule) since *since_ts*.

    ``{}`` when no store exists yet (nothing was ever recorded); ``None`` when a
    store exists but cannot be read — a reader must say "unavailable", never
    print a confident zero. A read never CREATES the store.
    """
    try:
        from core.event_log import open_event_log
        log = open_event_log(data_dir)
    except Exception:
        return None
    if log is None:
        return {}
    attrs_in: Dict[str, Any] = {"outcome": tuple(outcomes)}
    if autonomous_only:
        attrs_in["autonomous"] = (1,)
    total: Dict[str, int] = {}
    for uid in {str(user_id or ""), ""}:
        part = log.count_by_effect(since_ts=since_ts, user_id=uid, attrs_in=attrs_in)
        if part is None:
            return None
        for eff, n in part.items():
            total[eff] = total.get(eff, 0) + int(n)
    return total


def outward_line(counts: Optional[Dict[str, int]]) -> str:
    """The owner-facing phrase for :func:`outward_counts`' answer."""
    if counts is None:
        return "unavailable (event log unreadable)"
    return effect_line(counts) or "none"


__all__ = [
    "ACTION_EFFECTS", "EFFECT_CLASSES", "EFFECT_VERBS", "ENVELOPE_KEYS",
    "EffectVerdict", "GATED_EFFECTS", "NON_WRITE_ACTIONS", "OUTCOMES",
    "TOKEN_TO_EFFECT", "WRITE_ACTIONS", "ceiling", "classify_effect", "dedup_key",
    "effect_line", "effect_pause_refusal", "external_write_pause_gate_enabled",
    "external_write_strict", "external_write_telemetry_enabled", "outward_counts",
    "outward_line", "pause_kind_for", "record_external_write",
]


# 067 P4 prerequisite: the three row views, built on first read.
__getattr__ = lazy_module_getattr(__name__, {
    "NON_WRITE_ACTIONS": lambda: _row_views()[0],
    "WRITE_ACTIONS": lambda: _row_views()[1],
    "ACTION_EFFECTS": lambda: _row_views()[2],
})
