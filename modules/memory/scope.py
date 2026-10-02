"""Memory scopes — a second partition axis INSIDE a tenant (quarantine, then promote).

Every goal run, cron run and owner chat of one tenant used to read and write ONE
pool (``memories`` in ``memory.db``, keyed by ``user_id`` only). A goal that read
hostile content could store it as a "finding", and the next owner chat recalled
it. This module names the policy that stops that:

* a **scope label** is SERVER-minted — ``goal:<root_goal_id>`` or
  ``cron:<job_id>``; ``''`` is the shared pool. The agent never supplies one.
* a **regime** is a session's read/write policy over scopes:

  ===========  ============================  ===================
  regime       reads                         writes
  ===========  ============================  ===================
  ``shared``   the shared pool only          the shared pool
  ``scoped``   the shared pool + own scope   own scope only
  ``sealed``   own scope only                own scope only
  ===========  ============================  ===================

* **promotion**: a scoped goal's rows stay invisible to every other session until
  the ROOT goal of the scope completes ``verified``; then one bounded, idempotent
  UPDATE moves them to the shared pool. A failed / blocked / cancelled goal never
  promotes; the curator purges an orphaned scope after :data:`RETENTION_DAYS`.

Master switch ``MEMORY_SCOPES_ENABLED`` (default OFF). OFF is byte-identical to
the legacy store: no read predicate, no scoped write, no spec is ever built. The
deployment default regime is ``AUTONOMY_MEMORY_REGIME`` (default ``scoped``); it
applies to goal and cron runs only — owner chat is always ``shared``.

The session -> spec binding lives here (:func:`bind_session_scope`) rather than on
every call site: the registry routers already receive the ``session_id``, so the
per-step drain, the session-close drain, prefetch and ``session_search`` all pick
the scope up without a second plumbing path. A reader with no bound session (the
REPL ``/memory``, the console memory page) gets the shared-only view.

Flags are read via ``core.env`` inside ``modules/memory`` (this module never
imports ``agents.task``). Pure: stdlib + ``core.env``.
"""
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Optional, Tuple

from core.env import bool_env

logger = logging.getLogger(__name__)

REGIME_SHARED = "shared"
REGIME_SCOPED = "scoped"
REGIME_SEALED = "sealed"
#: Isolation order — a clamp takes the later of two.
REGIMES = (REGIME_SHARED, REGIME_SCOPED, REGIME_SEALED)

#: The deployment default regime when ``AUTONOMY_MEMORY_REGIME`` is unset
#: (owner decision 2026-09-23: ``scoped`` behind the master flag).
DEFAULT_REGIME = REGIME_SCOPED

#: Promotion policy: promote only when the completion judge said ``verified``
#: (with the judge disabled a recorded success counts as verified). A module
#: constant, not a flag — the flag-count ratchet asks for a reason per knob.
PROMOTE_POLICY = "verified"
#: Newest-first cap on the rows one promotion may move to the shared pool.
PROMOTE_MAX_ROWS = 50
#: The curator purges unpromoted scoped rows older than this.
RETENTION_DAYS = 14

_LABEL_RE = re.compile(r"^[a-z0-9:_-]{1,64}$")


@dataclass(frozen=True)
class MemoryScopeSpec:
    """One session's scope: the label it owns and the regime it runs under."""
    label: str
    regime: str

    @property
    def writes_scoped(self) -> bool:
        return self.regime in (REGIME_SCOPED, REGIME_SEALED) and bool(self.label)


def scopes_enabled() -> bool:
    """``MEMORY_SCOPES_ENABLED`` — the master switch (default OFF)."""
    return bool_env("MEMORY_SCOPES_ENABLED", False)


def normalize_regime(value) -> Optional[str]:
    """A valid regime name, lower-cased, or None."""
    v = str(value or "").strip().lower()
    return v if v in REGIMES else None


def default_regime() -> str:
    """``AUTONOMY_MEMORY_REGIME`` — the regime a goal / cron run gets when its
    payload names none. An unknown value is logged and falls back to the
    default (fail toward isolation, never toward the shared pool)."""
    raw = (os.getenv("AUTONOMY_MEMORY_REGIME") or "").strip()
    if not raw:
        return DEFAULT_REGIME
    v = normalize_regime(raw)
    if v is None:
        logger.warning("AUTONOMY_MEMORY_REGIME=%r is not one of %s — using %s",
                       raw, "|".join(REGIMES), DEFAULT_REGIME)
        return DEFAULT_REGIME
    return v


def clamp_regime(floor: Optional[str], requested: Optional[str]) -> str:
    """The more isolated of *floor* and *requested* (``shared < scoped < sealed``).

    A goal created by a session running under regime R can never be weaker than
    R — otherwise a quarantined session could mint a shared-writing child as an
    escape hatch."""
    a = normalize_regime(floor) or REGIME_SHARED
    b = normalize_regime(requested) or REGIME_SHARED
    return a if REGIMES.index(a) >= REGIMES.index(b) else b


def valid_label(label) -> bool:
    return bool(label) and bool(_LABEL_RE.match(str(label)))


def goal_label(root_goal_id: str) -> str:
    return f"goal:{str(root_goal_id or '').strip().lower()}"


def cron_label(job_id: str) -> str:
    return f"cron:{str(job_id or '').strip().lower()}"


def build_spec(label, regime) -> Optional[MemoryScopeSpec]:
    """A spec, or None when the flag is off, the regime is ``shared``/unknown, or
    the label fails the charset check (labels are server-minted; checked anyway)."""
    if not scopes_enabled():
        return None
    r = normalize_regime(regime)
    if r is None or r == REGIME_SHARED:
        return None
    if not valid_label(label):
        logger.warning("memory scope: refusing an invalid label %r", label)
        return None
    return MemoryScopeSpec(label=str(label), regime=r)


def request_fields(label: str, payload=None) -> dict:
    """The ``memory_scope`` / ``memory_regime`` keys a run request carries, or
    ``{}`` (flag off, or the run resolves to ``shared``). Regime:
    ``payload.memory_regime`` > :func:`default_regime`."""
    if not scopes_enabled():
        return {}
    requested = normalize_regime((payload or {}).get("memory_regime")) \
        if isinstance(payload, dict) else None
    regime = requested or default_regime()
    spec = build_spec(label, regime)
    if spec is None:
        return {}
    return {"memory_scope": spec.label, "memory_regime": spec.regime}


def goal_create_regime(requested, creator_session_id=None) -> Tuple[Optional[str], Optional[str]]:
    """``goal_create``'s regime: ``(value_to_store | None, error | None)``.

    * scopes OFF + a requested regime -> a structured refusal naming the switch
      (never silently ignored); scopes OFF + none -> nothing stored.
    * an unknown name -> refusal.
    * otherwise the request is CLAMPED to the creating session's own regime, so a
      quarantined run can never mint a shared-writing child. Nothing is stored
      when the result is the deployment default path (no request, shared creator).
    """
    creator = session_scope(creator_session_id)
    if requested is None or str(requested).strip() == "":
        if creator is None:
            return None, None
        return creator.regime, None
    if not scopes_enabled():
        return None, ("memory_regime needs memory scopes, which are off on this "
                      "instance (MEMORY_SCOPES_ENABLED). Omit memory_regime.")
    r = normalize_regime(requested)
    if r is None:
        return None, f"memory_regime must be one of {', '.join(REGIMES)}"
    return clamp_regime(creator.regime if creator else None, r), None


# ---- session binding --------------------------------------------------------

_BINDINGS_MAX = 4096
_bindings: "OrderedDict[str, MemoryScopeSpec]" = OrderedDict()
_lock = Lock()


def bind_session_scope(session_id: str, label=None, regime=None) -> Optional[MemoryScopeSpec]:
    """Bind *session_id* to a scope for the life of the process (bounded LRU).
    A None / shared / flag-off spec UNBINDS, so a reused id never keeps a stale
    scope. Returns the bound spec."""
    if not session_id:
        return None
    spec = build_spec(label, regime) if label else None
    with _lock:
        if spec is None:
            _bindings.pop(str(session_id), None)
            return None
        _bindings[str(session_id)] = spec
        _bindings.move_to_end(str(session_id))
        while len(_bindings) > _BINDINGS_MAX:
            _bindings.popitem(last=False)
    return spec


def session_scope(session_id) -> Optional[MemoryScopeSpec]:
    """The spec bound to *session_id*, or None (shared). Always None while the
    master flag is off — a binding made before a flip does not outlive it."""
    if not session_id or not scopes_enabled():
        return None
    with _lock:
        return _bindings.get(str(session_id))


def reset_bindings() -> None:
    """Test helper."""
    with _lock:
        _bindings.clear()


# ---- SQL fragments (the provider is the ONE enforcement point) ------------

def read_predicate(spec: Optional[MemoryScopeSpec], norm_user: str) -> Tuple[str, tuple]:
    """The extra ``WHERE`` fragment for a ``memories m`` query, or ``("", ())``
    when the flag is off. A row with no provenance sidecar row is shared."""
    if not scopes_enabled():
        return "", ()
    scoped_any = ("m.rowid IN (SELECT mem_rowid FROM mem_provenance "
                  "WHERE user_id = ? AND scope != '')")
    own = ("m.rowid IN (SELECT mem_rowid FROM mem_provenance "
           "WHERE user_id = ? AND scope = ?)")
    if spec is None or not spec.writes_scoped:
        return f" AND NOT {scoped_any}", (norm_user,)
    if spec.regime == REGIME_SEALED:
        return f" AND {own}", (norm_user, spec.label)
    return f" AND ({own} OR NOT {scoped_any})", (norm_user, spec.label, norm_user)


def vector_predicate(spec: Optional[MemoryScopeSpec]) -> Tuple[str, tuple]:
    """The ``mem_meta m`` twin of :func:`read_predicate`."""
    if not scopes_enabled():
        return "", ()
    if spec is None or not spec.writes_scoped:
        return " AND COALESCE(m.scope, '') = ''", ()
    if spec.regime == REGIME_SEALED:
        return " AND m.scope = ?", (spec.label,)
    return " AND (COALESCE(m.scope, '') = '' OR m.scope = ?)", (spec.label,)


def write_label(spec: Optional[MemoryScopeSpec]) -> str:
    """The scope a write lands in: the spec's label for scoped/sealed, else ''."""
    if spec is None or not scopes_enabled() or not spec.writes_scoped:
        return ""
    return spec.label


def telemetry_attrs(spec: Optional[MemoryScopeSpec]) -> dict:
    """``memory_scope`` / ``memory_regime`` attrs — never ``scope``, which the
    memory events already use for the recall SOURCE."""
    if spec is None:
        return {}
    return {"memory_scope": spec.label, "memory_regime": spec.regime}


__all__ = [
    "DEFAULT_REGIME", "MemoryScopeSpec", "PROMOTE_MAX_ROWS", "PROMOTE_POLICY",
    "REGIMES", "REGIME_SCOPED", "REGIME_SEALED", "REGIME_SHARED", "RETENTION_DAYS",
    "bind_session_scope", "build_spec", "clamp_regime", "cron_label", "default_regime",
    "goal_create_regime", "goal_label", "normalize_regime", "read_predicate", "request_fields",
    "reset_bindings", "scopes_enabled", "session_scope", "telemetry_attrs",
    "valid_label", "vector_predicate", "write_label",
]
