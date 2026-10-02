"""Contributed status sections (067 P5a) — core names the slot, a provider fills it.

``core.status_snapshot.SECTION_ORDER`` stays the ONE authoritative order and
the aggregation gate. Eight of its names are SLOTS that a rail (later: a pack)
fills through :func:`register_status_section`; every other name is built by
the snapshot itself. A provider cannot add a new name — a section outside
``SECTION_ORDER`` would contribute no health item and no unavailability (the
silent-omission class the status SSOT exists to prevent).

An empty slot renders ONE line, ``not installed — …``, in state ``ok``: absence
is stated, never omitted (the status-silence rule), and a feature nobody
installed does not mark every snapshot ``partial``.

Until the wallet leaves core (067 P5b) the current builders register from
their own modules; :data:`_IN_CORE_PROVIDERS` names them, and the snapshot
imports them on first build. P5b deletes a row when the pack registers that
slot instead.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: The SECTION_ORDER names a provider may fill (pinned against SECTION_ORDER by
#: tests/unit/core/test_status_sections.py).
CONTRIBUTED_SLOTS = ("room_actions", "creations", "collectibles", "liquidity",
                     "wallet", "custody", "money", "economics")

#: In-core provider module -> the slots it registers on import. Shrink-only as
#: P5b moves each builder into the wallet pack.
_IN_CORE_PROVIDERS: Dict[str, tuple] = {
    "core.status_room_actions": ("room_actions",),
    "core.status_money": ("creations", "collectibles", "wallet", "money"),
    "core.status_liquidity": ("liquidity",),
    "core.status_custody": ("custody",),
    "core.status_economics": ("economics",),
}

NOT_INSTALLED = "not installed — no installed pack provides this section"
NO_TENANT = "no tenant (empty user_id)"


@dataclass
class SectionContext:
    """What a slot builder may read. ``data_dir`` is already resolved.

    ``ledger`` is the ``build_ledger`` dict (or the exception it raised) the
    caller passed in; the ``money`` builder fills it when it reads one, and the
    ``economics`` builder (built after it, in SECTION_ORDER) reads it for the
    runway line."""
    uid: str
    data_dir: str
    now: float
    window_sec: int
    include_money: bool = True
    include_balances: bool = False
    ledger: Any = None
    liquidity_reader: Optional[Callable[..., Any]] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SectionProvider:
    name: str
    build: Callable[[SectionContext], Any]
    tenant: bool
    source: str


_PROVIDERS: Dict[str, SectionProvider] = {}
_IMPORT_ERRORS: Dict[str, str] = {}
_loaded = False


def register_status_section(name: str, build: Callable[[SectionContext], Any], *,
                            tenant: bool = True, source: str = "core") -> None:
    """Fill the slot ``name`` with ``build(ctx) -> Section``.

    ``tenant=True``: with no tenant the slot renders ``unavailable (no tenant
    (empty user_id))`` and ``build`` is not called. Last writer wins (a test or
    a later pack replaces a provider). A name core did not declare a slot is
    refused."""
    if name not in CONTRIBUTED_SLOTS:
        raise ValueError(
            f"status section {name!r} is not a slot: core owns SECTION_ORDER and "
            f"names the slots a provider may fill ({', '.join(CONTRIBUTED_SLOTS)})")
    if not callable(build):
        raise TypeError(f"status section {name!r}: {build!r} is not callable")
    _PROVIDERS[name] = SectionProvider(name, build, bool(tenant), str(source))


def provider(name: str) -> Optional[SectionProvider]:
    _load_in_core()
    return _PROVIDERS.get(name)


def _load_in_core() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module, slots in _IN_CORE_PROVIDERS.items():
        try:
            importlib.import_module(module)
        except Exception as exc:  # the slots render the reason, never vanish
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.error("status provider %s did not load: %s", module, reason)
            for slot in slots:
                _IMPORT_ERRORS[slot] = reason


def build_slot(name: str, ctx: SectionContext):
    """The slot's Section: built, ``unavailable`` (a failed provider or no
    tenant), or one ``not installed`` line. Never absent, never raises.

    The provider owns its section's state: this only turns a RAISE into
    ``unavailable(<reason>)``. A provider that wants the snapshot's
    "health ⇒ degraded" rule wraps its reader in ``status_snapshot._guarded``
    (every in-core provider does, except economics, whose runway item is added
    after the guard — as before 067 P5a)."""
    from core.status_snapshot import STATE_UNAVAILABLE, Section, _unavailable
    prov = provider(name)
    if prov is None:
        if name in _IMPORT_ERRORS:
            return Section(name=name, state=STATE_UNAVAILABLE, reason=_IMPORT_ERRORS[name])
        return Section(name=name, lines=[NOT_INSTALLED], data={"installed": False})
    if prov.tenant and not ctx.uid:
        return Section(name=name, state=STATE_UNAVAILABLE, reason=NO_TENANT)
    try:
        sec = prov.build(ctx)
    except Exception as exc:  # a provider fault is a visible, typed state
        logger.debug("status slot %s unavailable: %s", name, exc, exc_info=True)
        return _unavailable(name, exc)
    if getattr(sec, "name", None) != name:
        return Section(name=name, state=STATE_UNAVAILABLE,
                       reason=f"provider {prov.source!r} returned {type(sec).__name__}, "
                              f"not the {name!r} Section")
    return sec


__all__ = ["CONTRIBUTED_SLOTS", "NOT_INSTALLED", "SectionContext", "SectionProvider",
           "build_slot", "provider", "register_status_section"]
