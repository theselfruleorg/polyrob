"""The pack loader (067 P2): discover, validate, register — in two phases.

**Why two phases.** The policy views (``core.effects``, ``spend_lane``,
``approval``, ``room_policy``, the correspondent names, the capability-derived
sets, the CLI tables …) are snapshots of ``core.verb_policy`` and
``core.tool_capabilities``, built on first read (``core.lazy_views``). A pack's
rows must be in those tables before the views are built, yet a pack's CODE
may only run once the environment is loaded (the enabled set, the custody
check). So:

* **Phase 1 — :func:`register_policies`.** Runs at process entry (the CLI's
  ``main()``, the top of ``api/app.py`` and ``webview/server.py``), before views.
  It enumerates the ``polyrob.packs`` entry points, reads each ``pack.toml``
  WITHOUT importing the pack, validates it, resolves ``requires_packs`` and
  registers the capability rows and verb rows. It is environment-independent
  and runs no pack code, so it registers every installed pack's DATA —
  including a pack that phase 2 later leaves disabled or refuses (its tools
  never register, and :func:`core.packs.state.action_refusal` keeps their
  actions off every Controller).
* **Phase 2 — :func:`load_packs`.** Runs where tools are registered (the CLI
  container, ``initialize_tools``, the API app factory, a pack CLI command).
  It applies ``POLYROB_PACKS`` / ``POLYROB_PACKS_DISABLED`` and the custody rule,
  imports each enabled pack, calls ``pack()`` and contributes, in this order:
  tools (registrar + live gate) → hooks → CLI / API / status → skills scope.

**The guard.** ``core.verb_policy`` and ``core.tool_capabilities`` record every
view they build; a row that would belong to an already-built view is refused
there, and the loader turns that into this pack's named refusal. A late phase 1
therefore fails closed per pack — never a row silently missing from a gate.

Every refusal is per pack and named (``core.packs.state``); core itself never
fails because of a pack. ⚠️ The signer (``core/signer``,
``tools/defi/signer_entry.py``) must never import this module (ratcheted by
``tests/unit/core/packs/test_pack_ratchets.py``).
"""
import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from core.packs import index as _index
from core.packs import manifest as _manifest
from core.packs import state
from core.packs.spec import PackSpec, StatusSection, resolve_ref

logger = logging.getLogger(__name__)

GROUP = "polyrob.packs"

# A manifest's self-declared tier cannot grant the custody exemption. These
# are the core-reviewed distribution/entry-point identities: the first-party
# rows of the pack index (core/packs/index.json, 067 P7 — the ONE source).
# Artifact authenticity remains the trusted installer's job.
_FIRST_PARTY = _index.first_party_identities()


def _kill_refusal(pack_id: str, dist: Any, version: Optional[str]) -> Optional[str]:
    """The kill-list reason for this pack (by id and by distribution), or None."""
    name = dist.metadata.get("Name", "") if dist is not None else ""
    return _index.killed(pack_id, version) or (_index.killed(name, version) if name else None)


def kill_reason(rec: "state.PackRecord") -> Optional[str]:
    """The kill-list reason for an installed pack record (id and distribution,
    distribution and manifest versions), or None. Read by phase 2 and
    ``polyrob pack enable``."""
    versions = [v for v in (rec.dist_version, getattr(rec.manifest, "version", "")) if v]
    for name in filter(None, (rec.id, rec.dist_name)):
        for version in versions or [None]:
            reason = _index.killed(name, version)
            if reason:
                return reason
    return None


def _is_first_party_identity(ep) -> bool:
    """True when *ep* is exactly a core-reviewed (distribution, entry point) pair."""
    from packaging.utils import canonicalize_name
    dist = getattr(ep, "dist", None)
    name = dist.metadata.get("Name", "") if dist is not None else ""
    return _FIRST_PARTY.get(ep.name) == (canonicalize_name(name), ep.value)


def _first_party_refusal(rec, ep) -> Optional[str]:
    if rec.manifest.tier != "first-party":
        return None
    from packaging.utils import canonicalize_name
    dist = getattr(ep, "dist", None)
    name = dist.metadata.get("Name", "") if dist is not None else ""
    if _FIRST_PARTY.get(rec.id) != (canonicalize_name(name), ep.value):
        return ("first-party tier is reserved for core-reviewed distribution/entry-point "
                "identities; this manifest cannot grant itself the custody exemption")
    return None

#: Phase-1 registration order (topological over ``requires_packs``).
_ORDER: List[str] = []


def _entry_points() -> list:
    """The installed ``polyrob.packs`` entry points (a seam for the tests)."""
    from importlib.metadata import entry_points
    return list(entry_points(group=GROUP))


# --- phase 1 --------------------------------------------------------------------

#: 067 P5a: money rails that still live IN core and register their kernel hooks
#: (``core.money.hooks``) on package import. Phase 1 imports them at every
#: process entry so no money reader depends on who imported the rail first.
#: P5b deletes a row when that rail becomes a pack (the pack registers instead).
_IN_CORE_RAILS = ("core.wallet",)


def _register_in_core_rails() -> None:
    import importlib
    for name in _IN_CORE_RAILS:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — a rail must not stop core boot
            logger.error("in-core money rail %s did not register: %s", name, exc)


def _check_first_party_metadata() -> None:
    """The first-party packs ship inside ``polyrob``: when the ``polyrob``
    metadata in use declares none of their entry points, name it. 2026-09-25
    prod: a stale ``/opt/polyrob/polyrob.egg-info`` (PYTHONPATH=/opt/polyrob)
    shadowed the installed dist-info, so the service loaded no pack while a
    shell without that PYTHONPATH loaded all three. Running from a source tree
    with no metadata at all is not this fault and stays quiet."""
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        dist = distribution("polyrob")
    except PackageNotFoundError:
        return
    except Exception:  # noqa: BLE001
        return
    declared = {ep.name for ep in dist.entry_points if ep.group == GROUP}
    missing = sorted(set(_FIRST_PARTY) - declared)
    if not missing:
        return
    message = (f"first-party pack(s) {', '.join(missing)} have no entry point: the polyrob "
               f"metadata in use ({getattr(dist, '_path', 'unknown')}) does not declare them; "
               "a stale polyrob.egg-info on sys.path shadows the installed one — delete it, "
               "then pip install -e . (or pip install -U polyrob)")
    state.set_discovery_error(message)
    logger.error("pack discovery: %s", message)


def register_policies() -> None:
    """Phase 1 (once per process): read every installed pack's ``pack.toml`` and
    register its capability rows and verb rows. Imports no pack code."""
    if state.phase_done("policies"):
        return
    state.mark_phase("policies")
    _register_in_core_rails()
    try:
        # For a duplicated id the core-reviewed identity wins: a second
        # distribution declaring a first-party id must not shadow it by sorting
        # first (security assessment 2026-09-25).
        eps = sorted(_entry_points(),
                     key=lambda e: (e.name, not _is_first_party_identity(e), e.value))
    except Exception as exc:  # noqa: BLE001 — a broken metadata scan must not stop core
        state.set_discovery_error(f"{type(exc).__name__}: {exc}")
        logger.error("pack discovery failed: %s", exc)
        return
    _check_first_party_metadata()
    for ep in eps:
        if _retired_dist_entry(ep):
            continue
        if state.record(ep.name) is not None:
            logger.error("pack %r: a second entry point (%s) ignored", ep.name, ep.value)
            state.record(ep.name).errors.append(f"duplicate entry point {ep.value} ignored")
            continue
        rec = state.add(state.PackRecord(id=ep.name, entry_point=ep.value))
        dist = getattr(ep, "dist", None)
        reason = _kill_refusal(ep.name, dist, getattr(dist, "version", None)) if dist else None
        if reason:
            state.refuse(rec, reason)
            continue
        try:
            rec.manifest = _manifest.read(ep.module)
            if dist is not None:
                rec.dist_name, rec.dist_version = dist.metadata.get("Name", ""), dist.version
            reason = (_kill_refusal(ep.name, dist, rec.manifest.version)
                      or _static_refusal(rec) or _first_party_refusal(rec, ep))
        except Exception as exc:  # malformed metadata belongs to this pack only
            state.refuse(rec, f"pack.toml: {type(exc).__name__}: {exc}")
            continue
        if reason:
            state.refuse(rec, reason)
    for rec in _ordered(state.records()):
        _register_rows(rec)
    for rec in state.records():
        if rec.status == state.REFUSED:
            logger.error("pack %r refused: %s", rec.id, rec.reason)


def _retired_dist_entry(ep) -> bool:
    """067 (one install): an entry point of a RETIRED separate first-party pack
    distribution (``core.packs.index.RETIRED_DISTS`` — a leftover of the old
    layout, or a squatter's upload of that name) never loads. The pack polyrob
    ships carries a named error; alone, the id is refused. Both name the remedy."""
    dist = getattr(ep, "dist", None)
    name = (dist.metadata.get("Name", "") if dist is not None else "") or ""
    if not _index.retired_dist(name):
        return False
    # Never `pip uninstall`: a non-editable old wheel's RECORD lists pack files
    # the polyrob wheel owns now. THE helper removes metadata only.
    remedy = "python -m core.packs.retire"
    rec = state.record(ep.name)
    if rec is not None:
        rec.errors.append(f"a leftover {name} distribution also declares this pack and was "
                          f"ignored (the pack ships inside polyrob now); remedy: {remedy}")
    else:
        rec = state.add(state.PackRecord(id=ep.name, entry_point=ep.value))
        state.refuse(rec, f"distribution {name} is retired: first-party packs ship inside "
                          f"polyrob now; remedy: reinstall polyrob, then {remedy}")
    logger.error("pack %r: retired distribution %s ignored; remedy: %s", ep.name, name, remedy)
    return True


def _static_refusal(rec: "state.PackRecord") -> Optional[str]:
    m = rec.manifest
    if m.id != rec.id:
        return f"pack.toml id {m.id!r} differs from the entry point name {rec.id!r}"
    if m.requires_core:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
        from core.version import get_version
        try:
            ok = Version(get_version()) in SpecifierSet(m.requires_core, prereleases=True)
        except (InvalidSpecifier, InvalidVersion) as exc:
            return f"requires_core {m.requires_core!r} is not a version range ({exc})"
        if not ok:
            return (f"requires polyrob {m.requires_core}, this core is {get_version()}; "
                    f"install the pack version built for {get_version()}")
    if m.surfaces and m.tier != "first-party":
        return ("a third-party pack may not contribute a chat surface: the surface "
                "catalog's security columns (forgeable, alias_owner, owner_seat) need "
                "core review")
    derived = m.derived_capabilities()
    declared = m.capabilities - _manifest.CODE_CAPABILITIES
    if declared != derived:
        return (f"declared capabilities {sorted(declared)} differ from what its tool rows "
                f"carry {sorted(derived)}")
    return None


def _ordered(recs: Iterable["state.PackRecord"]) -> List["state.PackRecord"]:
    """Topological order over ``requires_packs``; a missing, refused or cyclic
    requirement refuses the dependent pack. Deterministic (by id)."""
    pending = {r.id: r for r in recs if r.status != state.REFUSED}
    refused = {r.id for r in recs if r.status == state.REFUSED}
    placed: List["state.PackRecord"] = []
    # First-party rows register first: a third-party pack must not refuse a
    # first-party one by claiming its tool id earlier (tier "first-party" here
    # is identity-checked — a self-declared claim was refused above).
    def _rank(pid: str):
        return (pending[pid].manifest.tier != "first-party", pid)

    while pending:
        progress = False
        for pid in sorted(pending, key=_rank):
            rec = pending[pid]
            deps = rec.manifest.requires_packs
            gone = [d for d in deps if d in refused or state.record(d) is None]
            if gone:
                dep = gone[0]
                what = "is not installed" if state.record(dep) is None else "is refused"
                state.refuse(rec, f"requires pack {dep!r}, which {what}")
                refused.add(pid)
                del pending[pid]
                progress = True
                break           # rescan from the highest-ranked pack
            elif all(d in {p.id for p in placed} for d in deps):
                placed.append(rec)
                del pending[pid]
                progress = True
                break
        if not progress:
            for pid in sorted(pending):
                state.refuse(pending[pid], f"requires_packs cycle among {sorted(pending)}")
            break
    return placed


def _register_rows(rec: "state.PackRecord") -> None:
    """All or nothing: validate every row of the pack, then register them."""
    from core.tool_capabilities import (register_tool_permissions, register_tool_row,
                                        validate_tool_permissions, validate_tool_row)
    from core.surfaces.catalog import register_surface, validate_surface
    from core.verb_policy import register_verb_policy, validate_verb_policy
    for dep in rec.manifest.requires_packs:
        dep_rec = state.record(dep)
        if dep_rec is None or dep_rec.status == state.REFUSED:
            state.refuse(rec, f"requires pack {dep!r}, which is refused")
            return
    source = f"pack:{rec.id}"
    tools = rec.manifest.tools
    try:
        # A pack may nest its own tool ids (markets: polymarket / polymarket_data,
        # 067 P4) as long as no action of the shorter id lies in the longer id's
        # namespace: every action row then names exactly one owning tool.
        for a in tools:
            for b in tools:
                if a.id != b.id and b.id.startswith(a.id + "_"):
                    stray = sorted(v.name for v in a.verbs if v.name.startswith(b.id + "_"))
                    if stray:
                        raise ValueError(f"tool {a.id!r} rows action(s) {stray} in the "
                                         f"action namespace of its tool {b.id!r}")
        for tool in tools:
            validate_tool_row(tool.id, tool.row, source=source)
            validate_verb_policy(tool.id, tool.verbs, source=source)
            if tool.permissions:
                validate_tool_permissions(tool.id, tool.permissions, source=source)
        package = _package(rec)
        for surface in rec.manifest.surfaces:
            validate_surface(surface, pack_id=rec.id, package=package)
            if surface.cli_command is not None and surface.id not in rec.manifest.cli_commands:
                raise ValueError(f"surface {surface.id!r} names a cli_command but "
                                 f"[cli] commands does not declare {surface.id!r}")
    except Exception as exc:  # invalid row types must also stay a per-pack refusal
        state.refuse(rec, f"policy rows refused: {exc}")
        return
    for tool in tools:
        register_tool_row(tool.id, tool.row, source=source)
        register_verb_policy(tool.id, tool.verbs, source=source)
        if tool.permissions:
            register_tool_permissions(tool.id, tool.permissions, source=source)
        state.own_tool(tool.id, rec.id)
    for surface in rec.manifest.surfaces:
        register_surface(surface, pack_id=rec.id, package=package)
    _ORDER.append(rec.id)


def _package(rec: "state.PackRecord") -> str:
    """The top-level package of the pack's entry point (``polyrob_x:pack`` -> ``polyrob_x``)."""
    return rec.entry_point.partition(":")[0].split(".", 1)[0]


# --- phase 2 --------------------------------------------------------------------

def csv_ids(raw: Optional[str]) -> List[str]:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


def enabled_set(recs: Iterable["state.PackRecord"]) -> Tuple[Set[str], Dict[str, str]]:
    """``(enabled ids, id -> why it is not enabled)`` from ``POLYROB_PACKS``
    (default: every installed first-party pack) and ``POLYROB_PACKS_DISABLED``."""
    recs = list(recs)
    named = csv_ids(os.environ.get("POLYROB_PACKS"))
    disabled = set(csv_ids(os.environ.get("POLYROB_PACKS_DISABLED")))
    why: Dict[str, str] = {}
    if named:
        enabled = set(named)
        for rec in recs:
            if rec.id not in enabled:
                why[rec.id] = "not named in POLYROB_PACKS"
    else:
        enabled = {r.id for r in recs if r.tier == "first-party"}
        for rec in recs:
            if rec.id not in enabled:
                why[rec.id] = "a third-party pack loads only when named in POLYROB_PACKS"
    for pid in enabled & disabled:
        why[pid] = "named in POLYROB_PACKS_DISABLED"
    return enabled - disabled, why


def custody_refusal() -> Optional[str]:
    """Apply the host-code boundary to packs, including remote signer access."""
    from core.security.host_execution import host_execution_refusal
    if host_execution_refusal() is None:
        return None
    return ("third-party code packs are refused while wallet custody or remote signing "
            "is enabled; run the pack in a separate instance and Python environment "
            "without signing credentials or access to the signer socket")


def load_packs() -> None:
    """Phase 2 (once per process): import each enabled pack and contribute its
    code. Call after the environment is loaded; runs phase 1 first if needed.
    Never raises: a pack's failure is its refusal, a loader fault is logged and
    shown as the discovery error."""
    try:
        register_policies()
        if state.phase_done("packs"):
            return
        state.mark_phase("packs")
        _load_enabled()
    except Exception as exc:  # noqa: BLE001 — a pack never stops core
        state.set_discovery_error(f"loader fault: {type(exc).__name__}: {exc}")
        logger.error("pack loading failed: %s", exc)


def _load_enabled() -> None:
    candidates = [state.record(pid) for pid in _ORDER]
    enabled, why = enabled_set(candidates)
    for rec in candidates:
        if rec.status != state.INSTALLED:
            continue
        if rec.id not in enabled:
            rec.status, rec.reason = state.DISABLED, why.get(rec.id, "not enabled")
            continue
        _load_one(rec)
    for rec in candidates:
        if rec.status == state.REFUSED:
            logger.error("pack %r refused: %s", rec.id, rec.reason)


def _load_one(rec: "state.PackRecord") -> None:
    for dep in rec.manifest.requires_packs:
        dep_rec = state.record(dep)
        if dep_rec is None or dep_rec.status != state.LOADED:
            status = dep_rec.status if dep_rec else "not installed"
            state.refuse(rec, f"requires pack {dep!r}, which is {status}")
            return
    # The kill list again, just before the pack's code is imported (067 P7).
    reason = kill_reason(rec)
    if reason:
        state.refuse(rec, reason)
        return
    if rec.tier != "first-party":
        reason = custody_refusal()
        if reason:
            state.refuse(rec, reason)
            return
    try:
        spec = resolve_ref(rec.entry_point)()
    except Exception as exc:  # noqa: BLE001 — a pack's import error refuses that pack
        state.refuse(rec, f"import failed: {type(exc).__name__}: {exc}")
        return
    try:
        commands = _check_spec(rec, spec)
    except Exception as exc:  # a referenced CLI module may raise during import
        state.refuse(rec, f"contract: {type(exc).__name__}: {exc}")
        return
    rec.spec, rec.commands = spec, commands
    from core.packs.sdk import needs
    rec.needs = needs(rec.manifest)
    try:
        _contribute(rec, spec)
    except Exception as exc:  # noqa: BLE001
        _neutralize(rec)
        state.refuse(rec, f"contribution failed: {type(exc).__name__}: {exc}")
        return
    rec.status = state.LOADED


def _check_spec(rec: "state.PackRecord", spec: Any) -> Dict[str, Any]:
    """Declared (``pack.toml``) against registered (``PackSpec``). Returns the
    resolved CLI commands by name."""
    m = rec.manifest
    if not isinstance(spec, PackSpec):
        raise TypeError(f"pack() returned {type(spec).__name__}, not a PackSpec")
    if spec.id != rec.id:
        raise ValueError(f"PackSpec id {spec.id!r} differs from the pack id {rec.id!r}")
    code_tools = sorted(t.id for t in spec.tools)
    data_tools = sorted(t.id for t in m.tools)
    if code_tools != data_tools:
        raise ValueError(f"tools in pack.toml {data_tools} differ from the tools pack() "
                         f"registers {code_tools}")
    unknown = sorted(set(spec.hooks) - set(HOOKS))
    if unknown:
        raise ValueError(f"unknown hook(s) {unknown} (pack_api 1 accepts {sorted(HOOKS)})")
    if ("api_routes" in m.capabilities) != bool(spec.api_routers):
        raise ValueError("declared capability api_routes differs from the routers pack() "
                         "returns")
    if ("console_routes" in m.capabilities) != bool(spec.console_routers):
        raise ValueError("declared capability console_routes differs from the console "
                         "routers pack() returns")
    if ("owner_verbs" in m.capabilities) != ("owner.verbs" in spec.hooks):
        raise ValueError("declared capability owner_verbs differs from the owner.verbs "
                         "hook pack() returns")
    if not all(isinstance(s, StatusSection) for s in spec.status_sections):
        raise TypeError("status_sections must hold StatusSection rows")
    commands = {}
    for ref in spec.cli:
        cmd = resolve_ref(ref)
        commands[getattr(cmd, "name", None)] = cmd
    if sorted(commands) != sorted(m.cli_commands):
        raise ValueError(f"cli commands in pack.toml {sorted(m.cli_commands)} differ from "
                         f"the commands pack() returns {sorted(map(str, commands))}")
    return commands


def _contribute(rec: "state.PackRecord", spec: PackSpec) -> None:
    from contextlib import ExitStack
    from core.boot_reconcilers import boot_reconciler, register_boot_reconciler
    from core.delivery_channels import unregister_pack
    from core.token_check_hook import token_checker, register_token_checker
    from core.tool_gates import register_gate
    from core.verbs import unregister_verbs
    for tool in spec.tools:
        if rec.withheld_need(tool.id) is not None:
            # 067 (one install): its SDK is absent — never registered, never
            # offered; its actions are refused with the remedy (state.action_refusal).
            register_gate(tool.id, lambda: False)
            continue
        resolve_ref(tool.registrar)()
        if tool.gate is not None:
            register_gate(tool.id, resolve_ref(tool.gate))
    # A later invalid hook must not leave a refused pack's earlier callbacks
    # live (especially auth checks and outbound cron delivery).
    with ExitStack() as rollback:
        rollback.callback(register_token_checker, token_checker())
        rollback.callback(unregister_pack, rec.id)
        rollback.callback(unregister_verbs, verb_source(rec.id))
        for tool in spec.tools:
            rollback.callback(register_boot_reconciler, tool.id, boot_reconciler(tool.id))
        for name, value in spec.hooks.items():
            HOOKS[name](rec, value)
        rollback.pop_all()


def rematerialize_tools() -> None:
    """Re-run every loaded pack's tool registrars under the CURRENT env (the
    CLI container, each ``register_cli_tools``): a self-gating registrar (e.g.
    the X pack's ``x_browser``, X_BROWSER_ENABLED) whose flag was off at
    phase 2 materializes its descriptor once the flag is on — as the core
    optional registrars do (``core.bootstrap._materialize_cli_optional_descriptors``).
    Registrars are idempotent; a failure is logged, never raised."""
    for rec in state.loaded():
        for tool in rec.spec.tools:
            if rec.withheld_need(tool.id) is not None:
                continue
            try:
                resolve_ref(tool.registrar)()
            except Exception as exc:  # noqa: BLE001
                logger.debug("pack %r: registrar for %r failed: %s", rec.id, tool.id, exc)


def _neutralize(rec: "state.PackRecord") -> None:
    """After a failed contribution: every tool of the pack reads as off."""
    from core.tool_gates import register_gate
    for tool in rec.manifest.tools:
        register_gate(tool.id, lambda: False)


# --- hooks ----------------------------------------------------------------------

def _hook_token_checker(rec: "state.PackRecord", value: Any) -> None:
    from core.token_check_hook import register_token_checker
    register_token_checker(resolve_ref(value))


def _hook_boot_reconciler(rec: "state.PackRecord", value: Any) -> None:
    """``{tool_id: coroutine function}`` — only for the pack's own tools."""
    from core.boot_reconcilers import register_boot_reconciler
    own = {t.id for t in rec.manifest.tools}
    for tool_id, fn in dict(value).items():
        if tool_id not in own:
            raise ValueError(f"boot reconciler for {tool_id!r}, which is not this pack's tool")
        register_boot_reconciler(tool_id, resolve_ref(fn))


def _hook_delivery_channel(rec: "state.PackRecord", value: Any) -> None:
    """``{channel name: sender}`` — only channels the pack's ``pack.toml``
    declares (``delivery_channels``), so the name is known before phase 2."""
    from core.delivery_channels import register_channel
    declared = set(rec.manifest.delivery_channels)
    for name, sender in dict(value).items():
        if name not in declared:
            raise ValueError(f"delivery channel {name!r} is not declared in pack.toml "
                             "delivery_channels")
        register_channel(name, resolve_ref(sender), pack_id=rec.id)


def verb_source(pack_id: str) -> str:
    """The ``core.verbs`` registration source of a pack's owner verbs."""
    return f"pack:{pack_id}"


def _hook_owner_verbs(rec: "state.PackRecord", value: Any) -> None:
    """``{"rows": (Verb, ...), "handlers": {seat: {name: "module:attr"}},
    "room_verbs": (...)}`` — owner slash verbs through the ONE verb table
    (``core.verbs.register_verbs``). The seats resolve the handler references;
    core never imports them. Needs the ``owner_verbs`` capability (phase 2)."""
    from core.verbs import register_verbs
    spec = dict(resolve_ref(value) if isinstance(value, str) else value)
    unknown = set(spec) - {"rows", "handlers", "room_verbs"}
    if unknown:
        raise ValueError(f"owner.verbs: unknown key(s) {sorted(unknown)}")
    rows = tuple(resolve_ref(r) if isinstance(r, str) else r for r in spec.get("rows", ()))
    if not rows:
        raise ValueError("owner.verbs: no rows")
    register_verbs(rows, spec.get("handlers") or {}, source=verb_source(rec.id),
                   room_verbs=tuple(spec.get("room_verbs") or ()))


#: hook name -> ``(record, value) -> None``. The seams core already owns.
HOOKS: Dict[str, Callable[["state.PackRecord", Any], None]] = {
    "identity.token_checker": _hook_token_checker,
    "autonomy.boot_reconciler": _hook_boot_reconciler,
    "cron.delivery_channel": _hook_delivery_channel,
    "owner.verbs": _hook_owner_verbs,
}


# --- read side ------------------------------------------------------------------

def api_routers() -> List[Tuple[str, Any]]:
    """``(pack id, router ref)`` of every loaded pack, in load order."""
    return [(rec.id, ref) for rec in state.loaded() for ref in rec.spec.api_routers]


def console_routers() -> List[Tuple[str, Any]]:
    """``(pack id, router ref)`` of every loaded pack's CONSOLE routers, in load
    order (``webview/pack_console.py`` mounts them under ``/api/packs/<id>``)."""
    return [(rec.id, ref) for rec in state.loaded() for ref in rec.spec.console_routers]


def console_public_paths() -> List[str]:
    """The FULL exact paths (``/api/packs/<id>/<path>``) a loaded pack declared
    public in ``[console] public_paths``. Only a loaded pack with console
    routers counts, so a refused or disabled pack opens nothing."""
    out: List[str] = []
    for rec in state.loaded():
        if not rec.spec.console_routers:
            continue
        out.extend(f"/api/packs/{rec.id}{p}" for p in rec.manifest.console_public_paths)
    return out


def reset_for_tests() -> None:
    state.reset()
    _ORDER.clear()


__all__ = ["GROUP", "HOOKS", "api_routers", "console_public_paths", "console_routers",
           "csv_ids", "custody_refusal", "enabled_set", "kill_reason", "load_packs",
           "register_policies", "verb_source"]
