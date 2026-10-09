"""067 P1: the ONE per-ACTION policy table.

Every per-action policy decision (the effect class of a write, ...) reads a row
here, keyed by the EXACT emitted action name: a container tool's action is
``{tool_id}_{action}`` (``tools/controller/tool_management.py``), a Controller
action with no owning tool is its bare name with ``tool=None``.

The lists that used to hold these names keep their public names as DERIVED
views (``core.effects.NON_WRITE_ACTIONS`` and the rest), so callers, tests and
monkeypatch targets are unchanged. Each view is LAZY (``core/lazy_views.py``,
067 P4 prerequisite): a module ``__getattr__`` builds it on first read and caches
it, so importing a view's module (even ``import core``) builds nothing. Decisions that read call
ARGUMENTS stay code in their modules and read the views: ``spend_exemption``,
the correspondent D1 reply exemption, the MCP ``readOnlyHint`` narrowing, the
``app_service`` -> ``app_deploy`` pause kind.

The rows are data in ``core/verb_policy_rows.py``. A later contribution (a pack)
adds its rows with :func:`register_verb_policy`, which refuses a name that is
already registered.

⚠️ A built view is a snapshot. Every query records the view it built
(:func:`ordered_ids_where` / :func:`ids_where`, keyed by call site), and a
registration whose row MATCHES an already-built view is refused with the name of
that view's module — a late row can never be silently missing from a gate. The
pack loader registers pack rows in its phase 1, at process entry, before any
view is first read (``core/packs/loader.py``).

``RESERVED_ACTION_NAMES``: names nothing emits today, kept on purpose and each
recorded with its reason in the rows module. They carry their policy, so every
decision that names them is unchanged; the parity ratchet
(``tests/unit/core/test_verb_policy.py``) accepts a row only for an emitted name
or a reserved one.

Tier-0: no imports beyond the stdlib and the data module.
"""
import sys
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple

#: The effect classes of an external write (proposal 033). ``core.effects``
#: re-exports this set.
EFFECT_CLASSES: FrozenSet[str] = frozenset({
    "social",    # public broadcast (a post the world can read)
    "comms",     # a directed message to an identified party
    "public",    # content or code made reachable at a public address
    "money",     # value movement, or granting a standing financial authority
    "code",      # arbitrary command / code execution
    "self",      # self-modification (skills, identity docs, own env, capability set)
    "network",   # arbitrary outbound egress whose payload the agent controls
})

#: ``effect`` has three states, as ``classify_effect`` always had: a class (an
#: explicit write row), ``none`` (explicitly NOT an external write) and
#: ``inherit`` (no row: a tool action writes at its tool's ceiling, a tool-less
#: action writes nothing).
EFFECT_VALUES: FrozenSet[str] = EFFECT_CLASSES | {"none", "inherit"}

#: The spend lane a money verb rides (``core/config_policy/spend_lane.py``):
#: ``defi`` = simulate-and-assert, exemptible below the autonomous ceiling
#: (``DEFI_SPEND_VERBS``); ``owner_always`` = never exemptible
#: (``ALWAYS_OWNER_APPROVED_VERBS``); ``x402`` = the x402 micro-payment lane
#: (``X402_SPEND_VERBS``).
LANES: FrozenSet[str] = frozenset({"none", "defi", "x402", "owner_always"})

#: Which side of a payment a payment-approval verb is on
#: (``core/config_policy/payment_tools.py``). ``receive`` alone may be
#: act-and-report under ``PAYMENT_APPROVAL_MODE=auto``.
SIDES: FrozenSet[Optional[str]] = frozenset({None, "spend", "receive"})

#: Who asks the owner before a money verb runs: ``hook`` = the shared
#: payment-approval pre-hook (``PAYMENT_APPROVAL_TOOLS``); ``verb`` = the verb
#: itself, at ``verb_gate`` (``VERB_OWNED_APPROVAL_GATES``).
APPROVAL_OWNERS: FrozenSet[Optional[str]] = frozenset({None, "hook", "verb"})

#: The generic approval lanes (``tools/controller/approval.py``); a verb may sit
#: on several. ``recommended`` = the ``DEFAULT_APPROVAL_REQUIRED_TOOLS`` preset
#: (opt-in; unioned at compute posture 2); ``posture2`` = the compute verbs gated
#: at ``AGENT_COMPUTE_POSTURE >= 2``; ``always_queued`` = owner-queued even under
#: ``AUTONOMY_MODE=autonomous`` (``_ALWAYS_GATED_VERBS``).
APPROVAL_LANES: FrozenSet[str] = frozenset({"recommended", "posture2", "always_queued"})


@dataclass(frozen=True, slots=True)
class VerbPolicy:
    """The policy of ONE emitted action name. Defaults mean "no policy"."""

    name: str
    tool: Optional[str] = None
    effect: str = "inherit"
    lane: str = "none"
    side: Optional[str] = None
    #: The param model has a ``dry_run`` field (default True): the call can be
    #: read as a simulation (``spend_lane.DRY_RUN_VERBS``). A POSITIVE flag: a
    #: verb without it is always gated as a live spend.
    simulatable: bool = False
    #: The verb can only retire exposure (``spend_lane._RISK_REDUCING_VERBS``).
    #: On the ``defi`` lane it tiers under ``DEFI_TIERED_SPEND_LANE``; on lane
    #: ``none`` (a venue cancel that moves no funds) it never waits on a tap.
    risk_reducing: bool = False
    #: The generic approval lanes (a subset of :data:`APPROVAL_LANES`).
    approval: FrozenSet[str] = frozenset()
    approval_owner: Optional[str] = None
    #: ``module::function`` of the gate a ``verb``-owned approval runs.
    verb_gate: str = ""
    #: Blocked while a session is tainted by correspondent DATA, by NAME alone
    #: (``correspondent_gate._HIGH_IMPACT_NAMES``); tool-id resolution and the
    #: prefix/substring layers are code in that module.
    correspondent_blocked: bool = False
    #: A room (public) turn may never call it, whoever spoke
    #: (``core/surfaces/room_policy.ROOM_DENIED_ACTIONS``).
    room_denied: bool = False

    def __post_init__(self) -> None:
        for field in ("simulatable", "risk_reducing", "correspondent_blocked", "room_denied"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"VerbPolicy {self.name}: {field} must be a boolean")
        if not self.name or self.name != self.name.strip():
            raise ValueError(f"VerbPolicy: bad action name {self.name!r}")
        if self.effect not in EFFECT_VALUES:
            raise ValueError(f"VerbPolicy {self.name}: unknown effect {self.effect!r}")
        object.__setattr__(self, "approval", frozenset(self.approval))
        if not self.approval <= APPROVAL_LANES:
            raise ValueError(f"VerbPolicy {self.name}: unknown approval lane(s) "
                             f"{sorted(self.approval - APPROVAL_LANES)}")
        if self.lane not in LANES:
            raise ValueError(f"VerbPolicy {self.name}: unknown lane {self.lane!r}")
        if self.side not in SIDES:
            raise ValueError(f"VerbPolicy {self.name}: unknown side {self.side!r}")
        if self.approval_owner not in APPROVAL_OWNERS:
            raise ValueError(f"VerbPolicy {self.name}: unknown approval_owner "
                             f"{self.approval_owner!r}")
        if bool(self.verb_gate) != (self.approval_owner == "verb"):
            raise ValueError(f"VerbPolicy {self.name}: verb_gate is set exactly when "
                             "approval_owner='verb'")
        if self.approval_owner is not None and self.side is None:
            raise ValueError(f"VerbPolicy {self.name}: an approval owner needs a side")
        if ((self.simulatable and self.lane not in {"defi", "owner_always"})
                or (self.risk_reducing and self.lane not in {"defi", "none"})):
            raise ValueError(f"VerbPolicy {self.name}: simulation needs a money lane; "
                             "risk_reducing needs the defi lane or none")

    def fields_set(self) -> Dict[str, Any]:
        """The non-default fields (``name``/``tool`` excluded)."""
        return {f.name: getattr(self, f.name) for f in fields(self)
                if f.name not in ("name", "tool") and getattr(self, f.name) != f.default}


_REGISTRY: Dict[str, VerbPolicy] = {}
_SOURCES: Dict[str, str] = {}

#: name -> :class:`VerbPolicy`, read-only. Registration order is preserved.
VERB_POLICY: Mapping[str, VerbPolicy] = MappingProxyType(_REGISTRY)


#: (file, line) of a query call site -> (reader module, predicate). One entry
#: per site, so a query made on every call stays bounded.
_BUILT_VIEWS: Dict[Tuple[str, int], Tuple[str, Dict[str, Any]]] = {}


def _note_view(frame, pred: Dict[str, Any]) -> None:
    code = frame.f_code
    _BUILT_VIEWS[(code.co_filename, frame.f_lineno)] = (
        frame.f_globals.get("__name__", code.co_filename), dict(pred))


def view_conflict(row: VerbPolicy) -> Optional[str]:
    """The module of an already-built view *row* would belong to, or None."""
    for reader, pred in _BUILT_VIEWS.values():
        if _matches(row, pred):
            return reader
    return None


def validate_verb_policy(tool_id: Optional[str], rows: Iterable[VerbPolicy], *,
                         source: str) -> list:
    """The checks of :func:`register_verb_policy` without registering: returns
    the rows, or raises ``ValueError`` naming the first refusal."""
    batch = list(rows)
    seen = set()
    for row in batch:
        if not isinstance(row, VerbPolicy):
            raise ValueError(f"{source}: {row!r} is not a VerbPolicy")
        if row.tool != tool_id:
            raise ValueError(f"{source}: row {row.name!r} names tool {row.tool!r}, "
                             f"registered under {tool_id!r}")
        if row.name in _REGISTRY:
            raise ValueError(f"{source}: action {row.name!r} already has a policy "
                             f"row from {_SOURCES[row.name]}")
        if row.name in seen:
            raise ValueError(f"{source}: action {row.name!r} is listed twice")
        seen.add(row.name)
        reader = view_conflict(row)
        if reader is not None:
            raise ValueError(f"{source}: action {row.name!r} would change a policy view "
                             f"{reader} already built; policy rows register before "
                             "that view is imported (067 P2 phase 1)")
    return batch


def register_verb_policy(tool_id: Optional[str], rows: Iterable[VerbPolicy], *,
                         source: str) -> None:
    """Add *rows* owned by *tool_id* (``None`` = tool-less Controller actions).

    All or nothing: a row whose ``tool`` is not *tool_id*, whose name is already
    registered (by any source) or repeated in *rows*, or that matches a view
    already built from this table, raises ``ValueError`` and nothing is added.
    A contribution never overrides a row.
    """
    batch = validate_verb_policy(tool_id, rows, source=source)
    for row in batch:
        _REGISTRY[row.name] = row
        _SOURCES[row.name] = source


def policy_for(name: str) -> Optional[VerbPolicy]:
    """The row for *name*, or None (every default)."""
    return _REGISTRY.get(name)


def _matches(row: VerbPolicy, pred: Dict[str, Any]) -> bool:
    for key, want in pred.items():
        value = getattr(row, key)
        if callable(want):
            if not want(value):
                return False
        elif value != want:
            return False
    return True


def _query(pred: Dict[str, Any]) -> Tuple[str, ...]:
    unknown = set(pred) - {f.name for f in fields(VerbPolicy)}
    if unknown:
        raise ValueError(f"ids_where: unknown VerbPolicy field(s) {sorted(unknown)}")
    return tuple(n for n, row in _REGISTRY.items() if _matches(row, pred))


def ordered_ids_where(**pred: Any) -> Tuple[str, ...]:
    """Names of the rows matching every ``field=value`` (or ``field=callable``)
    predicate, in registration order. Records the view (see the module doc)."""
    names = _query(pred)
    _note_view(sys._getframe(1), pred)
    return names


def ids_where(**pred: Any) -> FrozenSet[str]:
    """:func:`ordered_ids_where` as a frozenset."""
    names = _query(pred)
    _note_view(sys._getframe(1), pred)
    return frozenset(names)


def _load_core_rows() -> FrozenSet[str]:
    from core.verb_policy_rows import CORE_VERB_ROWS, RESERVED_VERB_ROWS
    for tool_id, rows in CORE_VERB_ROWS.items():
        register_verb_policy(
            tool_id, [VerbPolicy(name=n, tool=tool_id, **kw) for n, kw in rows.items()],
            source="core")
    register_verb_policy(
        None, [VerbPolicy(name=n, **kw) for n, kw in RESERVED_VERB_ROWS.items()],
        source="core:reserved")
    return frozenset(RESERVED_VERB_ROWS)


#: Names no action emits today, kept deliberately (reason per group in
#: ``core/verb_policy_rows.py``). Each has a row, registered with ``tool=None``.
RESERVED_ACTION_NAMES: FrozenSet[str] = _load_core_rows()


__all__ = [
    "EFFECT_CLASSES", "EFFECT_VALUES", "RESERVED_ACTION_NAMES", "VERB_POLICY",
    "VerbPolicy", "ids_where", "ordered_ids_where", "policy_for",
    "register_verb_policy", "validate_verb_policy", "view_conflict",
]
