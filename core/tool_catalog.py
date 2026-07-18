"""Product-facing tool catalog built from the descriptor source of truth."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_descriptors_module():
    """Load tools/descriptors.py without importing tools/__init__.py."""
    path = Path(__file__).resolve().parents[1] / "tools" / "descriptors.py"
    spec = importlib.util.spec_from_file_location("_polyrob_tool_descriptors", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load tool descriptors from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DESCRIPTORS = _load_descriptors_module()
TOOL_DESCRIPTORS = _DESCRIPTORS.TOOL_DESCRIPTORS
ToolCategory = _DESCRIPTORS.ToolCategory
ToolDescriptor = _DESCRIPTORS.ToolDescriptor


_CATEGORY_LABELS = {
    ToolCategory.CORE: "core",
    ToolCategory.BROWSER: "automation",
    ToolCategory.COMMUNICATION: "integration",
    ToolCategory.SEARCH: "automation",
    ToolCategory.VERIFICATION: "integration",
    ToolCategory.INTEGRATION: "integration",
}

# Folded into core/tool_capabilities.py (WS-2 tail, 2026-07-16): permissions live
# next to the capability rows and the risk tiers are DERIVED (high = external-write
# permission; medium = high_impact capability without one). These names stay as
# back-compat module attributes; classify a new tool THERE, not here.
from core.tool_capabilities import TOOL_PERMISSIONS as _TOOL_PERMISSIONS
from core.tool_capabilities import high_risk_tool_ids, medium_risk_tool_ids

_PERMISSIONS = {name: list(perms) for name, perms in _TOOL_PERMISSIONS.items()}
_HIGH_RISK_TOOLS = high_risk_tool_ids()
_MEDIUM_RISK_TOOLS = medium_risk_tool_ids()
_ENABLE_FLAGS = {
    "twitter": "TWITTER_ENABLED",
}
_DB_CREDENTIAL_TOOLS = {
    "polymarket": "requires database-stored trading credentials and explicit operator enablement",
    "hyperliquid": "requires database-stored trading credentials and explicit operator enablement",
}


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Stable product metadata for one tool."""

    id: str
    display_name: str
    category: str
    model_description: str
    human_description: str
    permissions: List[str] = field(default_factory=list)
    scope: str = "session"
    enabled: bool = True
    disabled_reason: Optional[str] = None
    required_config: List[str] = field(default_factory=list)
    required_services: List[str] = field(default_factory=list)
    optional_services: List[str] = field(default_factory=list)
    rate_limited: bool = False
    rate_limits: Dict[str, Any] = field(default_factory=dict)
    cost_risk: str = "low"
    security_risk: str = "low"
    audit_events: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_tool_catalog(env: Optional[Dict[str, str]] = None) -> List[ToolCatalogEntry]:
    """Return catalog entries sorted by category and id."""
    env = os.environ if env is None else env
    entries = [_entry_from_descriptor(name, desc, env) for name, desc in TOOL_DESCRIPTORS.items()]
    return sorted(entries, key=lambda e: (e.category, e.id))


def find_tool(tool_id: str, env: Optional[Dict[str, str]] = None) -> Optional[ToolCatalogEntry]:
    needle = tool_id.lower().strip()
    for entry in build_tool_catalog(env=env):
        if entry.id == needle:
            return entry
    return None


def permission_catalog() -> Dict[str, List[str]]:
    """Return permission class -> tool ids."""
    result: Dict[str, List[str]] = {}
    for entry in build_tool_catalog():
        for permission in entry.permissions:
            result.setdefault(permission, []).append(entry.id)
    return {key: sorted(value) for key, value in sorted(result.items())}


def _entry_from_descriptor(
    name: str,
    desc: ToolDescriptor,
    env: Dict[str, str],
) -> ToolCatalogEntry:
    required_config = list(desc.required_config)
    missing = [key for key in required_config if not _config_present(key, env)]
    gated_reason = _gated_reason(name, env)
    enabled = not missing
    disabled_reason = None
    if missing:
        disabled_reason = "missing config: " + ", ".join(missing)
    if gated_reason:
        enabled = False
        disabled_reason = gated_reason

    return ToolCatalogEntry(
        id=name,
        display_name=name.replace("_", " ").title(),
        category=_CATEGORY_LABELS.get(desc.category, desc.category.value),
        model_description=_model_description(desc.description),
        human_description=desc.description,
        permissions=list(_PERMISSIONS.get(name, [])),
        enabled=enabled,
        disabled_reason=disabled_reason,
        required_config=required_config,
        required_services=list(desc.required_services),
        optional_services=list(desc.optional_services),
        rate_limited=desc.rate_limited,
        rate_limits=dict(desc.rate_limit_settings),
        cost_risk=_risk_for(name),
        security_risk=_risk_for(name),
        audit_events=_audit_events(name),
    )


def _config_present(key: str, env: Dict[str, str]) -> bool:
    variants = {
        key,
        key.upper(),
        key.lower(),
        key.replace("_api_key", "_API_KEY").upper(),
    }
    return any(bool(env.get(variant)) for variant in variants)


def _model_description(description: str) -> str:
    first_sentence = description.split(".", 1)[0].strip()
    return first_sentence or description.strip()


def _risk_for(name: str) -> str:
    if name in _HIGH_RISK_TOOLS:
        return "high"
    if name in _MEDIUM_RISK_TOOLS:
        return "medium"
    return "low"


def _gated_reason(name: str, env: Dict[str, str]) -> Optional[str]:
    flag = _ENABLE_FLAGS.get(name)
    if flag and not _truthy(env.get(flag)):
        return f"gated by {flag}=false"
    return _DB_CREDENTIAL_TOOLS.get(name)


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _audit_events(name: str) -> List[str]:
    permissions = _PERMISSIONS.get(name, [])
    events = ["tool.call"]
    if any(p.endswith(".write") or p in {"social.post", "email.send", "trade.execute"} for p in permissions):
        events.append("external.write")
    return events
