"""``pack.toml`` — the data half of a pack's contract, read WITHOUT importing
the pack (067 P2; the lesson "inspect before load").

The file sits in the pack's top-level package, next to ``__init__.py``. It is
located with ``importlib.util.find_spec`` on the top-level name, which finds a
package without executing it (an installed wheel, an editable install and a
directory on ``sys.path`` alike).

Shape::

    id = "echo"
    version = "0.1.0"
    summary = "..."
    pack_api = 1
    requires_core = ">=1.0"          # a PEP 440 specifier set; "" = any
    requires_packs = []              # other pack ids, loaded first
    tier = "third-party"             # first-party requires a core-reviewed identity
    extra = ""                       # the polyrob extra carrying the pack's SDKs
                                     #  (first-party packs ship inside polyrob, 067)
    capabilities = ["tools"]         # tools money high_impact exec egress api_routes
                                     #  console_routes owner_verbs surface

    delivery_channels = []           # cron delivery channels the pack's hook sends on

    [cli]
    commands = ["echo"]              # names the pack's click commands answer to

    [console]                        # the console routers' public carve-out
    public_paths = ["/oauth/callback"]  # EXACT GET paths under /api/packs/<id>
                                     #  reachable without the owner's session
                                     #  (FIRST-PARTY only; needs console_routes)

    [surfaces.echo]                  # a chat-surface catalog row (FIRST-PARTY only;
    label = "Echo"                   #  core.surfaces.catalog.SurfaceSpec fields,
    module = "polyrob_echo.surface"  #  lists read as tuples; see register_surface)

    [tools.echo]                     # the capability row (core.tool_capabilities)
    capabilities = []                # money, high_impact, writes_*, ...
    untrusted_output = false
    cli = "none"                     # | "static" (the CLI serves it) | "incompatible"
    gate = { flag = "ECHO_ENABLED" } # optional core.tool_gates.ToolGate fields
    permissions = ["network.read"]   # optional catalog permissions (TOOL_PERMISSIONS)
    requires = ["echo_sdk"]          # optional import names the tool cannot work
                                     #  without (probed with find_spec, never imported);
                                     #  absent -> the tool is withheld and names the
                                     #  pack's `extra` (core.packs.sdk)

    [tools.echo.verbs.echo_say]      # one core.verb_policy.VerbPolicy per action
    effect = "none"
"""
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Tuple

from core.packs.spec import PACK_API_SUPPORTED
from core.tool_capabilities import KNOWN_CAPABILITIES, ToolRow
from core.tool_gates import ToolGate
from core.verb_policy import VerbPolicy

MANIFEST_NAME = "pack.toml"
TIERS = frozenset({"first-party", "third-party"})

#: The declared capability kinds. ``surface`` (067 P3b) = the pack contributes
#: chat-surface catalog rows (first-party only). ``console_routes`` = the pack
#: returns ``PackSpec.console_routers`` (mounted by the console, owner-only
#: except ``[console] public_paths``). ``owner_verbs`` = the pack registers
#: owner slash verbs through the ``owner.verbs`` hook (``core.verbs``). The
#: three code-half kinds (``api_routes``, ``console_routes``, ``owner_verbs``)
#: are checked against ``pack()`` in phase 2.
CAPABILITY_KINDS = frozenset({"tools", "money", "high_impact", "exec", "egress", "api_routes",
                              "surface", "console_routes", "owner_verbs"})

#: The capability kinds the CODE half carries (checked in phase 2, not against
#: the tool rows in phase 1).
CODE_CAPABILITIES = frozenset({"api_routes", "console_routes", "owner_verbs"})

#: One exact console path segment chain: ``/oauth/callback``. No query, no
#: parameter, no trailing slash, no dot segment — the public carve-out is an
#: exact string match and must stay one.
_PUBLIC_PATH_RE = re.compile(r"^(/[a-z0-9][a-z0-9_-]{0,63}){1,6}$")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CLI_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_EXTRA_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_TOP_KEYS = frozenset({"id", "version", "summary", "homepage", "license", "pack_api",
                       "requires_core", "requires_packs", "tier", "capabilities",
                       "flags_doc", "lazy_features", "cli", "tools", "surfaces",
                       "delivery_channels", "extra", "console"})
_TOOL_KEYS = frozenset({"capabilities", "untrusted_output", "cli", "gate", "permissions",
                        "verbs", "requires"})
#: A pack tool is registered by the loader's phase 2, never by the CLI's
#: row-derived registrars (``core.bootstrap``): no ``optional`` (it would need a
#: ``cli_registrar``). ``static`` = the CLI container serves the tool once phase 2
#: has put its descriptor in the init order (``core.bootstrap._CLI_STATIC_TOOLS``).
_PACK_CLI_MODES = frozenset({"none", "static", "incompatible"})


class ManifestError(ValueError):
    """``pack.toml`` is missing, unreadable or malformed."""


@dataclass(frozen=True)
class ToolPolicy:
    id: str
    row: ToolRow
    verbs: Tuple[VerbPolicy, ...]
    #: Catalog permission classes (``core.tool_capabilities.TOOL_PERMISSIONS``);
    #: empty = the tool has no catalog row.
    permissions: Tuple[str, ...] = ()
    #: Import names the tool cannot work without (``core.packs.sdk``); empty = none.
    requires: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    summary: str
    pack_api: int
    requires_core: str
    requires_packs: Tuple[str, ...]
    tier: str
    capabilities: FrozenSet[str]
    tools: Tuple[ToolPolicy, ...]
    cli_commands: Tuple[str, ...]
    path: Path
    #: Chat-surface catalog rows (``core.surfaces.catalog.SurfaceSpec``), 067 P3b.
    surfaces: Tuple[Any, ...] = ()
    #: Cron delivery channel names the pack's ``cron.delivery_channel`` hook serves.
    delivery_channels: Tuple[str, ...] = ()
    #: ``[console] public_paths``: exact GET paths under ``/api/packs/<id>``
    #: that the console serves WITHOUT the owner's session (first-party only).
    console_public_paths: Tuple[str, ...] = ()
    #: The ``polyrob[<extra>]`` that carries the pack's SDKs ("" = none). A
    #: first-party pack ships inside polyrob (067, one install); its SDKs do not.
    extra: str = ""

    def derived_capabilities(self) -> FrozenSet[str]:
        """The kinds the rows themselves carry (``api_routes`` is checked
        against the code half in phase 2)."""
        kinds = {"tools"} if self.tools else set()
        if self.surfaces:
            kinds.add("surface")
        for tool in self.tools:
            kinds |= {k for k in ("money", "high_impact", "exec") if k in tool.row}
            if "writes_network" in tool.row:
                kinds.add("egress")
        return frozenset(kinds)


def _console(data: Dict[str, Any], caps: FrozenSet[str], tier: str) -> Tuple[str, ...]:
    """``[console] public_paths`` — validated narrowly: exact paths, only with
    ``console_routes``, only for a first-party pack (the tier is identity-checked
    in phase 1, so a self-declared first-party pack is refused before this matters)."""
    console = data.get("console", {})
    if not isinstance(console, dict) or set(console) - {"public_paths"}:
        raise ManifestError("[console] holds only public_paths = [...]")
    paths = _str_list(console.get("public_paths", []), "console.public_paths")
    bad = [p for p in paths if not _PUBLIC_PATH_RE.match(p) or ".." in p]
    if bad:
        raise ManifestError(f"console public path(s) {bad} must be exact paths matching "
                            f"{_PUBLIC_PATH_RE.pattern}")
    if paths and "console_routes" not in caps:
        raise ManifestError("[console] public_paths needs the console_routes capability")
    if paths and tier != "first-party":
        raise ManifestError("a third-party pack may not declare public console paths: an "
                            "unauthenticated route on the owner's console needs core review")
    if len(set(paths)) != len(paths):
        raise ManifestError("[console] public_paths lists a path twice")
    return paths


def locate(module: str) -> Path:
    """``pack.toml`` of the top-level package of *module* (no import)."""
    top = module.split(".", 1)[0]
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError) as exc:
        raise ManifestError(f"package {top!r} cannot be located: {exc}") from exc
    if spec is None or not spec.submodule_search_locations:
        raise ManifestError(f"package {top!r} is not installed as a package")
    for base in spec.submodule_search_locations:
        path = Path(base) / MANIFEST_NAME
        if path.is_file():
            return path
    raise ManifestError(f"package {top!r} ships no {MANIFEST_NAME}")


def read(module: str) -> Manifest:
    """Locate, read and validate the manifest of the pack whose entry point
    lives in *module*. Raises :class:`ManifestError`."""
    import tomllib
    path = locate(module)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{path}: unreadable ({type(exc).__name__}: {exc})") from exc
    return parse(data, path)


def _str(data: Dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ManifestError(f"{key} must be a string")
    return value


def _str_list(value: Any, what: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{what} must be a list of strings")
    return tuple(value)


def parse(data: Dict[str, Any], path: Path) -> Manifest:
    """Validate a parsed ``pack.toml``. The ``pack_api`` check comes first, so a
    newer contract is refused as such rather than as unknown keys."""
    pack_api = data.get("pack_api")
    if not isinstance(pack_api, int) or isinstance(pack_api, bool):
        raise ManifestError("pack_api must be an integer")
    if pack_api not in PACK_API_SUPPORTED:
        raise ManifestError(f"pack_api {pack_api} is not supported by this core "
                            f"(supports {sorted(PACK_API_SUPPORTED)}); install a matching "
                            "polyrob or pack version")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ManifestError(f"unknown key(s) {sorted(unknown)}")
    pack_id = _str(data, "id")
    if not _ID_RE.match(pack_id):
        raise ManifestError(f"id {pack_id!r} must match {_ID_RE.pattern}")
    tier = _str(data, "tier", "third-party")
    if tier not in TIERS:
        raise ManifestError(f"tier {tier!r} not in {sorted(TIERS)}")
    caps = frozenset(_str_list(data.get("capabilities", []), "capabilities"))
    if caps - CAPABILITY_KINDS:
        raise ManifestError(f"capability kind(s) {sorted(caps - CAPABILITY_KINDS)} are not "
                            f"supported by pack_api 1 (known: {sorted(CAPABILITY_KINDS)})")
    cli = data.get("cli", {})
    if not isinstance(cli, dict) or set(cli) - {"commands"}:
        raise ManifestError("[cli] holds only commands = [...]")
    commands = _str_list(cli.get("commands", []), "cli.commands")
    bad = [c for c in commands if not _CLI_RE.match(c)]
    if bad:
        raise ManifestError(f"cli command name(s) {bad} must match {_CLI_RE.pattern}")
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        raise ManifestError("[tools] must be a table")
    surfaces = data.get("surfaces", {})
    if not isinstance(surfaces, dict):
        raise ManifestError("[surfaces] must be a table")
    channels = _str_list(data.get("delivery_channels", []), "delivery_channels")
    bad = [c for c in channels if not _ID_RE.match(c)]
    if bad:
        raise ManifestError(f"delivery channel name(s) {bad} must match {_ID_RE.pattern}")
    extra = _str(data, "extra")
    if extra and not _EXTRA_RE.match(extra):
        raise ManifestError(f"extra {extra!r} must match {_EXTRA_RE.pattern}")
    return Manifest(
        id=pack_id, version=_str(data, "version"), summary=_str(data, "summary"),
        pack_api=pack_api, requires_core=_str(data, "requires_core"),
        requires_packs=_str_list(data.get("requires_packs", []), "requires_packs"),
        tier=tier, capabilities=caps,
        tools=tuple(_tool(tid, spec) for tid, spec in tools.items()),
        cli_commands=commands, path=path,
        surfaces=tuple(_surface(sid, spec) for sid, spec in surfaces.items()),
        delivery_channels=channels, extra=extra,
        console_public_paths=_console(data, caps, tier))


def _tuples(value: Any) -> Any:
    """TOML arrays -> tuples (nested), the catalog's field shape."""
    if isinstance(value, list):
        return tuple(_tuples(v) for v in value)
    return value


def _surface(surface_id: str, spec: Any) -> Any:
    """``[surfaces.<id>]`` -> a ``SurfaceSpec`` (validated against the catalog
    when the loader registers it)."""
    from core.surfaces.catalog import SurfaceSpec
    if not isinstance(spec, dict):
        raise ManifestError(f"[surfaces.{surface_id}] must be a table")
    if {"id", "pack"} & set(spec):
        raise ManifestError(f"[surfaces.{surface_id}] may not set id or pack")
    fields = {k: _tuples(v) for k, v in spec.items()}
    # TOML has no null: an omitted Optional column is None (no extra, no owner env).
    fields.setdefault("extra", None)
    fields.setdefault("owner_env", None)
    try:
        return SurfaceSpec(id=surface_id, **fields)
    except TypeError as exc:
        raise ManifestError(f"surfaces.{surface_id}: {exc}") from exc


def _tool(tool_id: str, spec: Any) -> ToolPolicy:
    if not isinstance(spec, dict):
        raise ManifestError(f"[tools.{tool_id}] must be a table")
    unknown = set(spec) - _TOOL_KEYS
    if unknown:
        raise ManifestError(f"[tools.{tool_id}] unknown key(s) {sorted(unknown)}")
    caps = frozenset(_str_list(spec.get("capabilities", []), f"tools.{tool_id}.capabilities"))
    if caps - KNOWN_CAPABILITIES:
        raise ManifestError(f"tools.{tool_id}: unknown capability token(s) "
                            f"{sorted(caps - KNOWN_CAPABILITIES)}")
    cli = spec.get("cli", "none")
    if cli not in _PACK_CLI_MODES:
        raise ManifestError(f"tools.{tool_id}: cli must be one of {sorted(_PACK_CLI_MODES)}")
    gate = spec.get("gate")
    untrusted_output = spec.get("untrusted_output", False)
    if type(untrusted_output) is not bool:
        raise ManifestError(f"tools.{tool_id}: untrusted_output must be a boolean")
    try:
        row = ToolRow(caps, untrusted_output=untrusted_output,
                      cli=cli, gate=ToolGate(**gate) if gate is not None else None)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"tools.{tool_id}: {exc}") from exc
    verbs = spec.get("verbs", {})
    if not isinstance(verbs, dict) or not verbs:
        raise ManifestError(f"tools.{tool_id}: every tool classifies its actions "
                            "([tools.<id>.verbs.<action>])")
    rows = []
    for name, fields in verbs.items():
        if not name.startswith(tool_id + "_"):
            raise ManifestError(f"tools.{tool_id}: action {name!r} must be named "
                                f"'{tool_id}_<verb>' (the emitted name)")
        if not isinstance(fields, dict) or {"name", "tool"} & set(fields):
            raise ManifestError(f"tools.{tool_id}.verbs.{name} must be a table of policy fields")
        try:
            rows.append(VerbPolicy(name=name, tool=tool_id, **fields))
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"tools.{tool_id}.verbs.{name}: {exc}") from exc
    permissions = _str_list(spec.get("permissions", []), f"tools.{tool_id}.permissions")
    requires = _str_list(spec.get("requires", []), f"tools.{tool_id}.requires")
    bad = [m for m in requires if not _MODULE_RE.match(m)]
    if bad:
        raise ManifestError(f"tools.{tool_id}: requires {bad} must be import names")
    return ToolPolicy(id=tool_id, row=row, verbs=tuple(rows), permissions=permissions,
                      requires=requires)


__all__ = ["CAPABILITY_KINDS", "MANIFEST_NAME", "Manifest", "ManifestError", "TIERS",
           "ToolPolicy", "locate", "parse", "read"]
