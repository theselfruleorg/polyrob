"""Tool enable gates — the ONE gate module (067 P1).

Two halves, one module:

1. **Static declaration** — :class:`ToolGate`, carried as the ``gate`` field of the
   per-tool rows in ``core/tool_capabilities.py``. It names the flag and the
   agent-facing disclosure (label, tier ``loadable|disabled|reserved``, remedy).
   ``tools.controller.tool_load_report.TOOL_GATE_FLAGS`` and
   ``agents.task.agent.core.tool_availability.GATED_TOOL_REGISTRY`` are VIEWS of it.
2. **Runtime predicate** — ``tool_id -> () -> bool``. ``core`` and ``agents`` may not
   import ``tools`` (``tests/test_layering_ratchet.py``), yet several readers there
   need a tool's live enable gate (is ``anysite`` on? is ``hf_deploy`` on?). The gate
   function stays next to its tool; the tool module registers it here at import
   (:func:`register_gate`), and the lower tiers look it up by tool id.
   ``core.tool_capabilities`` re-exports these functions; the store lives here.

``import tools`` loads every registrar (``tools/__init__.py``). A reader that runs
with the tool tier not loaded gets ``None`` from :func:`gate_for`; it must treat
that as "the tool is not available", never as enabled.
"""
from dataclasses import dataclass
from typing import Callable, Dict, Optional

Gate = Callable[[], bool]

#: ``ToolGate.tier`` values (the <tool-availability> vocabulary).
GATE_TIERS = frozenset({"loadable", "disabled", "reserved"})


@dataclass(frozen=True)
class ToolGate:
    """A tool's static gate declaration (the ``gate`` field of its row).

    - ``flag`` — the env flag whose OFF value explains "this tool is not
      registered" (the ``TOOL_GATE_FLAGS`` view; checked against the flags catalog).
    - ``label``/``tier``/``remedy`` — the line disclosed to the agent in the
      <tool-availability> block (the ``GATED_TOOL_REGISTRY`` view); a gate with no
      ``tier`` is not disclosed.
    """

    flag: Optional[str] = None
    label: Optional[str] = None
    tier: Optional[str] = None
    remedy: Optional[str] = None

    def __post_init__(self):
        if self.tier is not None and self.tier not in GATE_TIERS:
            raise ValueError(f"tier={self.tier!r} not in {sorted(GATE_TIERS)}")
        if (self.tier is None) != (self.label is None) or (self.tier is None) != (self.remedy is None):
            raise ValueError("label, tier and remedy are declared together")

    def disclosure(self) -> Optional[tuple]:
        """``(label, tier, remedy)`` or ``None`` when the gate is not disclosed."""
        return None if self.tier is None else (self.label, self.tier, self.remedy)

_GATES: Dict[str, Gate] = {}


def register_gate(tool_id: str, fn: Gate) -> None:
    """Called from the tool tier at import. Last registration wins."""
    _GATES[tool_id] = fn


def gate_for(tool_id: str) -> Optional[Gate]:
    """The registered gate for ``tool_id``, or ``None`` when none is registered."""
    return _GATES.get(tool_id)


def gate_on(tool_id: str) -> bool:
    """``True`` only when a gate is registered AND it returns true.

    Fail-closed: no gate or a gate that raises is ``False``.
    """
    fn = _GATES.get(tool_id)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


__all__ = ["GATE_TIERS", "Gate", "ToolGate", "register_gate", "gate_for", "gate_on"]
