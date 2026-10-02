"""The code half of a pack's contract (067 P2): what ``pack()`` returns.

A pack is an installed distribution with an entry point in the
``polyrob.packs`` group (``echo = "polyrob_echo:pack"``). Its contract has two
halves, read at two different times (``core/packs/loader.py``):

* ``pack.toml`` — DATA, read without importing the pack
  (``core/packs/manifest.py``): identity, ``pack_api``, version ranges, tier,
  declared capabilities, the per-tool capability rows and per-action policy
  rows, the CLI command names. The loader registers the policy rows in its
  phase 1, at process entry, before any policy view is built.
* :class:`PackSpec` — CODE, returned by the entry point's callable in phase 2,
  once the environment is loaded and the pack is enabled: tool registrars and
  live gates, hooks, CLI commands, API routers, console routers, status
  sections, the skills directory.

Every reference to code is a callable or a ``"module:attr"`` string that the
loader resolves; core never imports a pack by name. No imports above core.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple, Union

#: ``"module:attr"`` or the object itself.
Ref = Union[str, Any]

#: The contract versions this core can load.
PACK_API_SUPPORTED = frozenset({1})


@dataclass(frozen=True)
class ToolContribution:
    """One tool the pack registers. Its capability row and verb rows are in
    ``pack.toml`` under ``[tools.<id>]`` — they register in phase 1.

    - ``registrar`` — zero-argument callable that registers the tool class
      (``tools.descriptors.register_optional_tool``) and returns whether it did.
    - ``gate`` — the live enable predicate (``() -> bool``) registered in
      ``core.tool_gates`` for the lower tiers; ``None`` = no live gate.
    """

    id: str
    registrar: Ref
    gate: Optional[Ref] = None


@dataclass(frozen=True)
class StatusSection:
    """A pack's part of the ``packs`` status section: ``build() -> list[str]``."""

    name: str
    build: Callable[[], Any]


@dataclass(frozen=True)
class PackSpec:
    """What a pack contributes in phase 2. ``id`` must equal the manifest id.

    ``hooks`` keys are the names in ``core.packs.loader.HOOKS``; an unknown key
    refuses the pack. ``cli`` holds click commands whose names equal the
    manifest's ``[cli] commands``. ``api_routers`` mount under
    ``/api/packs/<id>`` in the API app (``api/pack_routes.py``).

    ``console_routers`` mount under the SAME prefix in the console
    (``webview/pack_console.py``) — the process that serves the public HTTPS
    host. Every route is OWNER-ONLY there (the console's auth middleware, an
    owner guard, and the read-only + CSRF guards on a mutation) except the
    exact GET paths the manifest lists in ``[console] public_paths``
    (first-party only). Declaring ``console_routers`` requires the
    ``console_routes`` capability in ``pack.toml``.
    """

    id: str
    tools: Tuple[ToolContribution, ...] = ()
    hooks: Mapping[str, Any] = field(default_factory=dict)
    cli: Tuple[Ref, ...] = ()
    api_routers: Tuple[Ref, ...] = ()
    console_routers: Tuple[Ref, ...] = ()
    status_sections: Tuple[StatusSection, ...] = ()
    skills_dir: Optional[Path] = None


def resolve_ref(ref: Ref) -> Any:
    """``"module:attr"`` -> the attribute; anything else is returned as is."""
    if isinstance(ref, str):
        import importlib
        module, _, attr = ref.partition(":")
        if not module or not attr:
            raise ValueError(f"reference {ref!r} is not 'module:attr'")
        obj = importlib.import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj
    return ref


__all__ = ["PACK_API_SUPPORTED", "PackSpec", "Ref", "StatusSection", "ToolContribution",
           "resolve_ref"]
