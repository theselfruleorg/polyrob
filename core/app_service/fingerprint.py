"""032 — what the owner actually approved.

Approval must bind to the approved CONFIGURATION, not merely to the address.
Without this, an owner who approved ``hello-world`` (``egress='none'``, one
argv, no env) could have that same slug silently redeployed later with
``egress='open'`` and an exfiltration command — unattended, because the address
was already approved.

The fingerprint is a stable sha256 over the security-relevant fields only:

- ``cmd`` — what the container runs;
- ``container_port`` / ``health_path`` — what the supervisor publishes and
  fetches;
- ``egress`` + ``egress_allow`` — where the app may reach;
- the env KEY set — which variables the container receives.

``workspace_digest`` is deliberately ABSENT: a code bump on the SAME
configuration must still redeploy unattended (the documented behaviour the
``ship-software`` stream depends on), and the tree is separately gated by
``ship == tested``. Env VALUES are absent too — they are screened at the
registry (:mod:`core.app_service.env_scan`) and a value edit is not a change of
what the app may do.
"""
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Field order is the owner-facing order; also the diff order.
FINGERPRINT_FIELDS = ("cmd", "container_port", "health_path", "egress", "egress_allow",
                      "env_keys")

_LABELS = {
    "cmd": "the start command",
    "container_port": "the container port",
    "health_path": "the health path",
    "egress": "the egress mode",
    "egress_allow": "the egress allow-list",
    "env_keys": "the env variable names",
}


def approval_config(*, cmd: Sequence[str], container_port: Any, health_path: Any,
                    egress: Any, egress_allow: Optional[Sequence[str]],
                    env: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The normalized, order-insensitive view the fingerprint hashes."""
    try:
        port = int(container_port)
    except (TypeError, ValueError):
        port = 0
    return {
        "cmd": [str(c) for c in (cmd or [])],
        "container_port": port,
        "health_path": str(health_path or "/"),
        "egress": str(egress or "none"),
        "egress_allow": sorted({str(h).strip().lower() for h in (egress_allow or [])}),
        "env_keys": sorted({str(k) for k in (env or {})}),
    }


def config_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The approval config a (parsed) registry row currently describes."""
    return approval_config(
        cmd=row.get("cmd") or [],
        container_port=row.get("container_port") or 0,
        health_path=row.get("health_path"),
        egress=row.get("egress"),
        egress_allow=row.get("egress_allow"),
        env=row.get("env"),
    )


def fingerprint(config: Mapping[str, Any]) -> str:
    """Stable sha256 of an approval config."""
    canonical = json.dumps({k: config.get(k) for k in FINGERPRINT_FIELDS},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_of_row(row: Mapping[str, Any]) -> str:
    return fingerprint(config_from_row(row))


def changed_fields(approved: Optional[Mapping[str, Any]],
                   requested: Mapping[str, Any]) -> List[str]:
    """Which fingerprint fields differ. ``[]`` when *approved* is unknown."""
    if not isinstance(approved, Mapping):
        return []
    return [f for f in FINGERPRINT_FIELDS if approved.get(f) != requested.get(f)]


def describe_change(fields: Sequence[str]) -> str:
    """Owner-readable: WHICH fields moved since the approval."""
    named = [_LABELS.get(f, f) for f in fields]
    if not named:
        return "the approved configuration changed"
    if len(named) == 1:
        return f"{named[0]} changed"
    return ", ".join(named[:-1]) + f" and {named[-1]} changed"
