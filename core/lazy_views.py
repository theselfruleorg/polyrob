"""067 P4 prerequisite: policy views built on FIRST USE, not at import.

A derived policy view (``PAYMENT_APPROVAL_TOOLS``, ``UNTRUSTED_TOOL_NAMESPACES``
…) is a snapshot of ``core.verb_policy`` / ``core.tool_capabilities``. Built at
import, it froze as soon as ``import core`` ran, and the pack loader's phase 1
then refused every pack row that view would include. A lazy view is computed by
a PEP 562 module ``__getattr__`` the first time anything reads the attribute,
and cached in the module globals, so every later read is a plain attribute.

The guards are unchanged: the builder calls ``ids_where`` / ``ordered_ids_where``
from its own module, so the built view is recorded under that module at the
moment it is built, and a later row it would include is still refused by name.

Callers keep every public name: ``module.NAME``, ``from module import NAME``
(which builds the view at that importer's import), ``monkeypatch.setattr``.
⚠️ Inside the owning module, a function must read the view with
:func:`view` (a bare global name raises ``NameError`` before the first build).

Tier-0: stdlib only.
"""
import importlib
import sys
from typing import Any, Callable, Dict, Iterable


def lazy_module_getattr(module_name: str, builders: Dict[str, Callable[[], Any]],
                        reexports: Dict[str, str] = None) -> Callable[[str], Any]:
    """A module ``__getattr__`` that builds ``builders[name]()`` or re-reads
    ``name`` from the module ``reexports[name]`` on first access, and caches the
    value in *module_name*'s globals."""
    reexports = dict(reexports or {})

    def __getattr__(name: str) -> Any:
        if name in builders:
            value = builders[name]()
        elif name in reexports:
            value = getattr(importlib.import_module(reexports[name]), name)
        else:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        sys.modules[module_name].__dict__.setdefault(name, value)
        return sys.modules[module_name].__dict__[name]

    return __getattr__


def reexport_map(source: str, names: Iterable[str]) -> Dict[str, str]:
    """``{name: source}`` for :func:`lazy_module_getattr`'s *reexports*."""
    return {n: source for n in names}


def view(module_name: str, name: str) -> Any:
    """The current value of the lazy view *name* (built on first read). Honors a
    monkeypatched module attribute."""
    return getattr(sys.modules[module_name], name)


__all__ = ["lazy_module_getattr", "reexport_map", "view"]
