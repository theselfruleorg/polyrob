"""The reason and the source of one act (proposal 122 W1).

A write verb takes an optional ``why`` (one line, at most 200 characters). The Controller
binds it here for the action in flight (``Controller.multi_act``), so every reader of the
act sees the SAME line with no verb plumbing it through:

- ``core.wallet.tx_guard.authorize`` puts it on the ``TxIntent`` (``intent.why``), and
  :func:`autonomous_why_refusal` refuses an AUTONOMOUS money act that has none. An
  owner-direct or owner-approved act may omit it: the owner is the reason.
- ``core.wallet.tx_notify`` puts it on the ``TxNotice``: the owner reads the lane and the
  reason on the money notice.
- the effect recorder (``tools/controller/effect_hooks.py``) puts it on the act record
  (the ``external_write`` row), with the short params and the source of the decision.

A code-run act (the income rule's buy) binds its source the same way. ContextVars are per task: a
sub-agent in its own task binds its own value. Fail-open: a reader that cannot read the
binding sees no reason, and the guard then refuses an autonomous money act (fail closed).
"""
import json
import re
from contextvars import ContextVar, Token
from typing import Any, Mapping, Optional

WHY_MAX = 200

#: The description every ``why`` param carries, so the model reads one rule everywhere.
WHY_DESCRIPTION = ("Why this act, now (one line). Required on an autonomous run; the "
                   "owner reads it.")

_WHY: ContextVar[str] = ContextVar("polyrob_act_why", default="")
_SOURCE: ContextVar[str] = ContextVar("polyrob_act_source", default="")


def clean_why(why: Optional[str]) -> str:
    """The reason as it is stored and shown: one line, secret shapes scrubbed, capped."""
    from core.model_routing import clean_why as _clean
    return _clean(why)


#: A ``/word`` that may be a tap token (``/card_<id>_ok``, ``/nav_<id>``, ``/approve_p_<id>``).
_SLASH_WORD = re.compile(r"/[A-Za-z0-9_]+")


def _untapped(match: "re.Match") -> str:
    token = match.group(0)
    try:
        from core.surfaces.tappable import is_tappable_token
        return "(tap removed)" if is_tappable_token(token) else token
    except Exception:
        return "(tap removed)"


def shown_why(why: Optional[str]) -> str:
    """The reason as an owner notice shows it: a code span (a Markdown link inside renders as
    text, a backtick cannot close it) with every tap token removed — the agent wrote the line,
    and a ``/card_<id>_ok`` of another open card must not be one tap away. ``""`` when there
    is no reason (audit 2026-11-21 L-2)."""
    try:
        text = clean_why(why)
    except Exception:
        return ""
    if not text:
        return ""
    text = _SLASH_WORD.sub(_untapped, text).replace("`", "'")
    return f"`{text}`"


def set_act_why(why: Optional[str]) -> Token:
    """Bind the reason of the action in flight (``""`` clears it)."""
    try:
        text = clean_why(why)
    except Exception:
        text = ""
    return _WHY.set(text)


def reset_act_why(token: Token) -> None:
    try:
        _WHY.reset(token)
    except Exception:
        _WHY.set("")


def current_act_why() -> str:
    try:
        return _WHY.get()
    except Exception:
        return ""


def with_bound_why(intent: Any) -> Any:
    """*intent* (a ``TxIntent``) with the bound reason when it states none. Anything that
    is not a dataclass with a ``why`` field comes back unchanged."""
    try:
        import dataclasses
        if getattr(intent, "why", None) or not dataclasses.is_dataclass(intent) \
                or "why" not in {f.name for f in dataclasses.fields(intent)}:
            return intent
        why = current_act_why()
        return dataclasses.replace(intent, why=why) if why else intent
    except Exception:
        return intent


def set_act_source(source: Optional[str]) -> Token:
    """Bind who decided the act in flight when no turn says it (``income rule <id>``)."""
    return _SOURCE.set(" ".join(str(source or "").split())[:80])


def reset_act_source(token: Token) -> None:
    try:
        _SOURCE.reset(token)
    except Exception:
        _SOURCE.set("")


def grant_mandate(metadata: Optional[Mapping[str, Any]]) -> str:
    """The mandate id on the owner grant stamped for this call (``tx_guard.OWNER_GRANT_KEY``),
    or ``""`` — an act a mandate covers rides the owner's grant, yet the agent decided it."""
    try:
        from core.wallet.tx_guard import OWNER_GRANT_KEY
        grant = (metadata or {}).get(OWNER_GRANT_KEY)
        return str(grant.get("mandate") or "").strip()[:40] if isinstance(grant, Mapping) else ""
    except Exception:
        return ""


def act_source(session_id: Optional[str], *, autonomous: bool,
               metadata: Optional[Mapping[str, Any]] = None) -> str:
    """Who decided this act: ``income rule <id>`` · ``mandate <id>`` (a standing mandate's grant, or a run's mandate) · ``job <id>`` ·
    ``goal <id>`` · ``owner`` · ``agent`` (an autonomous turn with no job or goal)."""
    try:
        bound = _SOURCE.get()
        if bound:
            return bound
        standing = grant_mandate(metadata)
        if standing:
            return f"mandate {standing}"
        mandate = str((metadata or {}).get("mandate") or "").strip()
        if mandate:
            return f"mandate {mandate}"[:80]
        if not autonomous:
            return "owner"
        from core.autonomy_marker import cron_job_for_session, goal_for_session
        job = cron_job_for_session(session_id)
        if job:
            return f"job {job}"
        goal = goal_for_session(session_id)
        return f"goal {goal}" if goal else "agent"
    except Exception:
        return "agent" if autonomous else "owner"


#: Param names whose value is a body or a secret: the record keeps only its length.
_BODY_KEYS = frozenset({"text", "texts", "body", "content", "message", "html", "caption",
                        "prompt", "code", "script", "data", "calldata", "init_code"})
_SECRET_KEYS = frozenset({"password", "secret", "api_key", "private_key", "seed",
                          "mnemonic", "otp", "passphrase", "credential"})
_VALUE_MAX = 64
SHORT_PARAMS_MAX = 240


def short_params(params: Any) -> str:
    """The act's params in one short line for the record: scalars up to 64 characters, a
    body as its length, a secret as ``<redacted>``, the ``why`` left out (it has its own
    field). Never the content of a post or a mail."""
    try:
        if hasattr(params, "model_dump"):
            params = params.model_dump()
        if not isinstance(params, Mapping):
            return ""
        out = {}
        for key, value in params.items():
            if key == "why" or value in (None, "", [], {}):
                continue
            k = str(key).lower()
            if k in _SECRET_KEYS:
                out[key] = "<redacted>"
            elif k in _BODY_KEYS:
                out[key] = f"<{len(str(value))} chars>"
            elif isinstance(value, (bool, int, float)):
                out[key] = value
            else:
                s = " ".join(str(value).split())
                out[key] = s if len(s) <= _VALUE_MAX else s[:_VALUE_MAX - 1] + "…"
        line = json.dumps(out, sort_keys=True, ensure_ascii=False, default=str)
        return line if len(line) <= SHORT_PARAMS_MAX else line[:SHORT_PARAMS_MAX - 1] + "…"
    except Exception:
        return ""


WHY_REFUSAL = ("refused: an autonomous money act needs a reason — call it again with "
               "`why` (one line: why this act, now). Nothing was sent.")


def autonomous_why_refusal(lane: str, execution_context: Any,
                           why: Optional[str] = None) -> Optional[str]:
    """The refusal when an AUTONOMOUS money act has no reason, else None.

    An act a standing mandate covers rides the ``owner_approved`` lane, yet the agent chose
    it: it is held to the same rule (:func:`grant_mandate`; audit 2026-11-21 L-3).
    Only an agent turn is judged (a context exists): the remote signer re-runs the guard
    with no context and the agent side already held the act to this rule."""
    if execution_context is None:
        return None
    if lane != "autonomous" and not (
            lane == "owner_approved"
            and grant_mandate(getattr(execution_context, "metadata", None))):
        return None
    reason = why if why is not None else current_act_why()
    return None if clean_why(reason) else WHY_REFUSAL


__all__ = ["SHORT_PARAMS_MAX", "WHY_DESCRIPTION", "WHY_MAX", "WHY_REFUSAL", "act_source",
           "autonomous_why_refusal", "grant_mandate", "clean_why", "current_act_why", "reset_act_source",
           "reset_act_why", "set_act_source", "set_act_why", "short_params", "shown_why",
           "with_bound_why"]
