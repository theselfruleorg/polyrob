"""045 lane 1 — the perimeter traffic ledger.

Before this module, ``route_inbound`` denied silently: several branches return
``RouteDecision(RouteKind.DENIED, …, silent=True)`` with no log line at all, and
``core/event_kinds.py`` had no inbound kind. The question "who talked to the
agent yesterday and who was turned away" was unanswerable from any store.

Two hard rules, both pinned by tests:

1. **No body text, ever.** A security ledger that quotes messages is a second
   copy of every conversation. Length and an 8-char hash only — enough to count
   a flood, never enough to read it.
2. **Fail-open, always.** A raising event log must leave the caller's outcome
   byte-identical. A logging fault that changes a routing decision would be a
   worse bug than the blindness this module fixes.
3. **The row's tenant is the DEPLOYMENT, never the counterparty.** The sender
   lives in ``attrs.sender``. Writing the sender into ``user_id`` is how this
   lane spent its first fifteen commits writing rows no owner seat could read
   — see :func:`_owner_tenant`.
"""
import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The CLOSED attribute allowlist. Anything not here is not written. Pinned by
#: tests/unit/core/surfaces/test_access_log.py::test_attrs_are_a_closed_allowlist.
ROUTE_ATTRS = frozenset({
    "surface", "chat_id", "chat_type", "sender", "tier", "decision", "reason",
    "mentioned", "body_len", "body_sha8",
})


def body_digest(text: Optional[str]) -> tuple:
    """(length, 8-char sha256 prefix). The hash makes a repeated flood countable
    without retaining what was said."""
    s = text or ""
    return len(s), hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:8]


def _log():
    from core.event_log import get_event_log
    return get_event_log()


def _enabled() -> bool:
    from core.event_log import event_log_enabled
    from core.security_flags import security_event_log_enabled
    return security_event_log_enabled() and event_log_enabled()


#: Resolved once per process — the owner binding is env-derived and stable, and
#: this sits on the per-message hot path under group load. ``None`` = not yet
#: resolved (a test that changes the owner env resets it to ``None``).
_OWNER_TENANT: Optional[str] = None


def _owner_tenant() -> str:
    """The DEPLOYMENT tenant every perimeter row is stamped with.

    ⚠️ The row's ``user_id`` is the DEPLOYMENT, never the counterparty. The
    lane used to stamp ``identity.user_id`` — the SENDER's surface-hashed
    ``u_…`` id for any non-owner — while ``core/security_digest.py`` filters
    ``AND user_id = ?`` with the OWNER's id. The result was exactly backwards:
    a stranger's traffic was written to the store and then invisible on every
    owner seat, so a store holding a hostile inbound, a denial and an allowlist
    drop rolled up as ``inbound=1 denied=0`` — the owner's own messages and
    nothing else. The counterparty keeps its own home in ``attrs.sender``.

    Never raises and never costs a lookup twice: an unresolvable owner degrades
    to ``""`` (the row is still written — a tenant-less row is recoverable, a
    dropped row is not) and is NOT cached, so a transient fault does not pin the
    blindness in for the life of the process.
    """
    global _OWNER_TENANT
    if _OWNER_TENANT is None:
        try:
            from core.instance import resolve_owner_user_id
            _OWNER_TENANT = str(resolve_owner_user_id() or "")
        except Exception:
            logger.debug("access_log: owner tenant unresolved", exc_info=True)
            return ""
    return _OWNER_TENANT


def _write(kind: str, attrs: dict) -> None:
    clean = {k: v for k, v in attrs.items() if k in ROUTE_ATTRS}
    _log().record(kind, user_id=_owner_tenant(), source="perimeter", attrs=clean)


def record_route(inbound: Any, decision: Any, *, tier: Optional[str] = None) -> None:
    """Record ONE routing decision — allowed and denied alike.

    ``tier`` defaults to the decision's own resolved tier (``RouteDecision.tier``)
    so the PRODUCTION call site records one without passing anything; an explicit
    ``tier=`` still wins for a direct caller.
    """
    try:
        if not _enabled():
            return
        from core.event_kinds import ACCESS_DENIED, INBOUND_ROUTED
        ident = getattr(inbound, "identity", None)
        src = getattr(ident, "source", None)
        kind_val = getattr(getattr(decision, "kind", None), "value", None) \
            or str(getattr(decision, "kind", "") or "")
        body_len, sha8 = body_digest(getattr(inbound, "text", ""))
        denied = kind_val == "denied"
        if tier is None:
            tier = getattr(decision, "tier", None)
        _write(
            ACCESS_DENIED if denied else INBOUND_ROUTED,
            {
                "surface": getattr(src, "surface_id", "?"),
                "chat_id": getattr(src, "chat_id", "?"),
                "chat_type": getattr(src, "chat_type", "dm") or "dm",
                "sender": getattr(ident, "raw_user_id", None)
                          or getattr(ident, "user_id", "") or "",
                "tier": tier,
                "decision": kind_val,
                "reason": getattr(decision, "reason", None),
                "mentioned": getattr(inbound, "mentions_bot", None),
                "body_len": body_len,
                "body_sha8": sha8,
            },
        )
    except Exception:
        logger.debug("access_log: record_route skipped", exc_info=True)


def record_pre_route_drop(*, surface: str, chat_id: str, chat_type: str,
                          sender: str, reason: str, body_len: int = 0) -> None:
    """Record a drop that happens BEFORE ``route_inbound`` is ever called.

    Telegram's raw ``ALLOWED_TELEGRAM_USER_IDS`` gate refuses in
    ``handle_update`` and returns ``{"ok": True}``; under group load those drops
    are the bulk of hostile traffic and route_inbound never sees one of them.
    """
    try:
        if not _enabled():
            return
        from core.event_kinds import ACCESS_DENIED
        _write(ACCESS_DENIED, {
            "surface": surface, "chat_id": str(chat_id), "chat_type": chat_type,
            "sender": str(sender), "tier": "denied", "decision": "denied",
            "reason": reason, "body_len": int(body_len), "body_sha8": "",
        })
    except Exception:
        logger.debug("access_log: record_pre_route_drop skipped", exc_info=True)
