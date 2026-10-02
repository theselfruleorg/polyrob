"""What the pack loader found, per process (067 P2): installed, enabled,
loaded, refused — each with its reason. Read by ``polyrob pack``, the
``packs`` status section, ``polyrob doctor``, the CLI (pack commands), the API
(pack routers), the skill manager (pack skill scopes) and the Controller (a
pack tool's actions register only while its pack is loaded).

A refused pack is NAMED here with its reason — never silently absent.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: ``PackRecord.status`` values.
INSTALLED = "installed"   # policy rows registered, code not (yet) loaded
LOADED = "loaded"         # phase 2 done: tools, hooks, surfaces contributed
DISABLED = "disabled"     # installed, not in the enabled set
REFUSED = "refused"       # a named, fail-closed refusal (see ``reason``)


@dataclass
class PackRecord:
    id: str
    entry_point: str                  # "module:attr"
    manifest: Any = None              # core.packs.manifest.Manifest, when readable
    status: str = INSTALLED
    reason: str = ""
    spec: Any = None                  # core.packs.spec.PackSpec, once loaded
    commands: Dict[str, Any] = field(default_factory=dict)   # CLI name -> click command
    errors: List[str] = field(default_factory=list)   # non-fatal (a router that failed)
    dist_name: str = ""               # the installed distribution's Name, when known
    dist_version: str = ""            # ... and its version (the kill list reads both)
    #: 067 (one install): tools whose SDK is absent here (``core.packs.sdk.Need``),
    #: decided in phase 2. A withheld tool never registers; its remedy is named.
    needs: List[Any] = field(default_factory=list)

    def current_needs(self) -> List[Any]:
        """The SDK needs to show: phase 2's decision once loaded; before phase 2
        ran in this process (a status read, ``polyrob doctor``) the same probe
        over the manifest (``core.packs.sdk``, no import) — never an empty claim."""
        if self.status == LOADED:
            return list(self.needs)
        if self.status == INSTALLED and self.manifest is not None:
            try:
                from core.packs.sdk import needs
                return needs(self.manifest)
            except Exception:  # noqa: BLE001 — a probe fault shows nothing, never raises
                return []
        return []

    def withheld_need(self, tool_id: str) -> Any:
        """The :class:`core.packs.sdk.Need` that withholds *tool_id*, or None."""
        return next((n for n in self.needs if n.tool == tool_id and n.withheld), None)

    @property
    def tier(self) -> str:
        return getattr(self.manifest, "tier", "") or "unknown"

    @property
    def version(self) -> str:
        return getattr(self.manifest, "version", "") or "?"

    def line(self) -> str:
        head = f"{self.id} {self.version} ({self.tier}): {self.status}"
        if self.reason:
            head += f" — {self.reason}"
        found = self.current_needs()
        if found:
            from core.packs.sdk import describe
            head += f" — {describe(found)}"
        if self.errors:
            head += " · " + "; ".join(self.errors)
        return head


_RECORDS: Dict[str, PackRecord] = {}
#: tool id -> pack id, for every pack whose policy rows registered.
_TOOL_OWNER: Dict[str, str] = {}
#: The loader's phases that have run in this process.
_PHASES: Dict[str, bool] = {"policies": False, "packs": False}
#: Why discovery itself failed (the entry-point scan raised), or "".
_DISCOVERY_ERROR: List[str] = []


def records() -> List[PackRecord]:
    return list(_RECORDS.values())


def record(pack_id: str) -> Optional[PackRecord]:
    return _RECORDS.get(pack_id)


def add(rec: PackRecord) -> PackRecord:
    _RECORDS[rec.id] = rec
    return rec


def refuse(rec: PackRecord, reason: str) -> None:
    rec.status, rec.reason = REFUSED, reason


def own_tool(tool_id: str, pack_id: str) -> None:
    _TOOL_OWNER[tool_id] = pack_id


def pack_of_tool(tool_id: str) -> Optional[str]:
    return _TOOL_OWNER.get(tool_id)


def tool_withheld(tool_id: str) -> bool:
    """A pack tool whose pack phase 2 did NOT load (disabled or refused).

    False before phase 2 has decided (the pack may still load — a view built
    WHILE a pack loads must keep its tools) and for core tools.
    Read by the named tool profiles (``core.config_policy.profiles.provided``) so
    a session's default list never names a tool that cannot register."""
    pack_id = _TOOL_OWNER.get(tool_id)
    if pack_id is None or not _PHASES["packs"]:
        return False
    rec = _RECORDS.get(pack_id)
    return rec is None or rec.status in (DISABLED, REFUSED) or rec.withheld_need(tool_id) is not None


def loaded() -> List[PackRecord]:
    return [r for r in _RECORDS.values() if r.status == LOADED]


def phase_done(name: str) -> bool:
    return _PHASES[name]


def mark_phase(name: str) -> None:
    _PHASES[name] = True


def set_discovery_error(message: str) -> None:
    _DISCOVERY_ERROR[:] = [message]


def discovery_error() -> str:
    return _DISCOVERY_ERROR[0] if _DISCOVERY_ERROR else ""


def reset() -> None:
    """Forget everything (tests). Registered policy rows are NOT unregistered."""
    _RECORDS.clear()
    _TOOL_OWNER.clear()
    _DISCOVERY_ERROR.clear()
    for key in _PHASES:
        _PHASES[key] = False


def action_refusal(tool_id: str, action: str) -> Optional[str]:
    """Why a pack tool's *action* may not register on a Controller, or None.

    Core tools answer None. A pack tool's action registers only while its pack
    is loaded AND the action has a policy row (an unclassified verb would skip
    every per-action gate)."""
    pack_id = _TOOL_OWNER.get(tool_id)
    if pack_id is None:
        return None
    rec = _RECORDS.get(pack_id)
    if rec is None or rec.status != LOADED:
        status = rec.status if rec else "unknown"
        return f"pack {pack_id!r} is {status}" + (f" ({rec.reason})" if rec and rec.reason else "")
    need = rec.withheld_need(tool_id)
    if need is not None:
        return (f"tool {tool_id!r} of pack {pack_id!r} is withheld: "
                f"{', '.join(need.missing)} is not installed; remedy: {need.remedy()}")
    from core.verb_policy import policy_for
    if policy_for(action) is None:
        return f"action {action!r} of pack {pack_id!r} has no policy row in its pack.toml"
    return None


def cli_command_owners() -> Dict[str, str]:
    """CLI command name -> pack id, from the manifests (no pack import)."""
    out: Dict[str, str] = {}
    for rec in _RECORDS.values():
        if rec.manifest is not None and rec.status != REFUSED:
            for name in rec.manifest.cli_commands:
                out.setdefault(name, rec.id)
    return out


def skill_dirs() -> List[Tuple[str, Path]]:
    """``(pack id, skills dir)`` of every loaded pack that ships skills."""
    out = []
    for rec in loaded():
        root = getattr(rec.spec, "skills_dir", None)
        if root is not None and Path(root).is_dir():
            out.append((rec.id, Path(root)))
    return out


def summary_line() -> str:
    """One line for ``polyrob doctor``."""
    recs = records()
    if not _PHASES["policies"]:
        return "packs: not discovered in this process"
    if _DISCOVERY_ERROR:
        return f"packs: ! discovery failed ({_DISCOVERY_ERROR[0]})"
    if not recs:
        return "packs: none installed"
    counts: Dict[str, int] = {}
    for rec in recs:
        counts[rec.status] = counts.get(rec.status, 0) + 1
    body = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    refused = [f"{r.id} ({r.reason})" for r in recs if r.status == REFUSED]
    line = f"packs: {body}"
    if refused:
        line += " — ! refused: " + "; ".join(refused)
    needing = [(r, r.current_needs()) for r in recs]
    needing = [(r, n) for r, n in needing if n]
    if needing:
        from core.packs.sdk import describe
        line += " — " + "; ".join(f"{r.id}: {describe(n)}" for r, n in needing)
    return line


__all__ = ["DISABLED", "INSTALLED", "LOADED", "REFUSED", "PackRecord", "action_refusal",
           "add", "cli_command_owners", "discovery_error", "loaded", "mark_phase", "own_tool", "pack_of_tool",
           "phase_done", "record", "records", "refuse", "reset", "set_discovery_error", "skill_dirs",
           "summary_line", "tool_withheld"]
