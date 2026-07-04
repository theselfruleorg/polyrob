"""event_registry.py — the typed-event descriptor registry (D2, extensibility).

The CLI's render pipeline is ``feed_dict → normalize() → SessionState.update()
→ Renderer dispatch``. Adding a brand-new core event type used to require edits
across five files (a dataclass, a normalize branch, the type union, a state
branch, and a branch in EACH renderer). This registry collapses that to **one
``register_event(...)`` call**: a spec declares how the event parses, how it
mutates state, which layer it renders in, and how it renders — and the existing
``normalize``/``SessionState.update``/``Renderer`` dispatch consult the registry
automatically.

So a capability the core releases later (a new tool, a new autonomy signal, a
``payment_made`` event) surfaces in the UI with near-zero bespoke wiring, and
falls back to the harmless ``Info`` line if no spec is registered.

Design notes:
- The built-in first-class events (Step/LLMCall/…) keep their dedicated typed
  pipeline — they are well-tested and byte-identical. The registry is the
  EXTENSION seam (and the uniform path any future migration of built-ins would
  use). A registered type wins over the built-in fallback in ``normalize``.
- ``render_line`` returns a plain ``str`` (or ``None``) — renderer-neutral, so
  the base ``Renderer`` realizes it once (Rich styles it; Plain writes it) with
  zero per-renderer edits. This is what makes "new event = one registration".
- Everything here is pure + global-registry; ``unregister_event`` exists for
  test isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cli.ui.state import SessionState


class Layer(str, Enum):
    """Which composition layer a registered event renders in.

    - ``DIALOG``  — always rendered (the agent's voice / first-class signal).
    - ``ACTIVITY``— feeds the live status region / turn residue; not a scrollback
                    line by itself (state-only unless a ``render_line`` is given).
    - ``TRACE``   — rendered only under ``/verbose`` (demoted scaffolding).
    """

    DIALOG = "dialog"
    ACTIVITY = "activity"
    TRACE = "trace"


@dataclass
class RegisteredEvent:
    """The typed event produced by a registered spec's ``parse``.

    Carries the registry ``type`` (so dispatch can find the spec again), the
    parsed ``data`` payload, and the original ``raw`` feed dict.
    """

    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


def _noop_apply(state: "SessionState", event: RegisteredEvent) -> None:  # pragma: no cover
    return None


@dataclass(frozen=True)
class EventSpec:
    """One declarative event descriptor.

    Attributes:
        type:        The feed ``type`` string this spec handles.
        parse:       ``feed_dict -> RegisteredEvent`` (typed extraction).
        apply:       ``(state, event) -> None`` state mutation (default no-op).
        layer:       Where it renders (DIALOG / ACTIVITY / TRACE).
        render_line: ``event -> str | None`` — a renderer-neutral line. ``None``
                     (or a None return) renders nothing (e.g. ACTIVITY events
                     that only touch state).
    """

    type: str
    parse: Callable[[Dict[str, Any]], RegisteredEvent]
    apply: Callable[["SessionState", RegisteredEvent], None] = _noop_apply
    layer: Layer = Layer.TRACE
    render_line: Optional[Callable[[RegisteredEvent], Optional[str]]] = None


_REGISTRY: Dict[str, EventSpec] = {}


def register_event(spec: EventSpec) -> None:
    """Register (or replace) the spec for ``spec.type``."""
    _REGISTRY[spec.type] = spec


def unregister_event(type_str: str) -> None:
    """Remove a spec (test-isolation seam). No-op if absent."""
    _REGISTRY.pop(type_str, None)


def get_spec(type_str: str) -> Optional[EventSpec]:
    """Return the spec for ``type_str``, or ``None``."""
    return _REGISTRY.get(type_str)


def registered_types() -> FrozenSet[str]:
    """The set of currently-registered event type strings."""
    return frozenset(_REGISTRY)
