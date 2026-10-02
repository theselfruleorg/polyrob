"""A pack's optional SDKs (067, one install): which tools cannot work here, and
the one remedy for each.

The first-party packs ship INSIDE the ``polyrob`` distribution; their SDKs do
not. A ``pack.toml`` names the core extra that carries them (``extra =``) and
each tool lists the import names it cannot work without (``requires =``). This
module answers, WITHOUT importing anything (``importlib.util.find_spec``):

* :func:`needs` — per tool of a manifest, what is missing and the remedy
  (``pip install 'polyrob[<extra>]'``, ``core.optional_extras.pip_hint``);
* a need is **withheld** (the loader never registers the tool, its actions are
  refused with the remedy, the agent is never offered it) unless the extra
  has a trusted lazy feature and lazy installs are on here — then the tool is
  offered and installs on first use (``core.lazy_deps``), and the need is only
  reported.

It is THE source for the pack's "needs" state: ``polyrob pack list|doctor``,
the ``packs`` status section, ``polyrob doctor`` and the environment facts
(``core.install_facts``) all read it.
"""
from dataclasses import dataclass
from typing import Any, List, Tuple


@dataclass(frozen=True)
class Need:
    tool: str
    extra: str                  # the pack's extra ("" when the manifest names none)
    missing: Tuple[str, ...]    # the absent import names
    lazy: bool                  # True = installs on first use; the tool is still offered

    @property
    def withheld(self) -> bool:
        return not self.lazy

    def remedy(self) -> str:
        if self.extra:
            from core.optional_extras import pip_hint
            return pip_hint(self.extra)
        return "pip install " + " ".join(self.missing)


def _present(module: str) -> bool:
    from core.optional_extras import _spec_present
    return _spec_present(module)


def _lazy(extra: str) -> bool:
    """The extra installs on first use in this process (a trusted lazy feature)."""
    if not extra:
        return False
    try:
        from core.lazy_deps import feature_for_extra, lazy_installs_enabled
        return feature_for_extra(extra) is not None and lazy_installs_enabled()
    except Exception:  # noqa: BLE001 — unknown = not lazy: withhold, name the remedy
        return False


def needs(manifest: Any) -> List[Need]:
    """Every tool of *manifest* whose ``requires`` are not all importable here."""
    out: List[Need] = []
    extra = getattr(manifest, "extra", "") or ""
    for tool in getattr(manifest, "tools", ()) or ():
        missing = tuple(m for m in getattr(tool, "requires", ()) if not _present(m))
        if missing:
            out.append(Need(tool=tool.id, extra=extra, missing=missing, lazy=_lazy(extra)))
    return out


def describe(found: List[Need]) -> str:
    """One clause for a pack's state line ("" when nothing is missing)."""
    parts = []
    held = [n for n in found if n.withheld]
    lazy = [n for n in found if n.lazy]
    for remedy in dict.fromkeys(n.remedy() for n in held):
        tools = ", ".join(n.tool for n in held if n.remedy() == remedy)
        parts.append(f"needs `{remedy}` (tools withheld: {tools})")
    for remedy in dict.fromkeys(n.remedy() for n in lazy):
        tools = ", ".join(n.tool for n in lazy if n.remedy() == remedy)
        parts.append(f"{tools} installs on first use (`{remedy}`)")
    return "; ".join(parts)


__all__ = ["Need", "describe", "needs"]
