"""Reconcile-on-boot (proposal §3.5): re-health-check every ``live`` row so a
Space that died, was deleted out-of-band, or bit-rotted eventually flips to
``failed`` in the registry instead of staying an honest lie forever.

Fail-open per row: a ``http_get`` that raises (network down, DNS failure) never
mutates that row — silence is not evidence of death. Only an explicit non-2xx
response flips ``live`` -> ``failed``. Never touches ``pending``/``approved``/
``failed``/``undeployed`` rows.
"""
import asyncio
from typing import Callable, Optional

from tools.hf_deploy.registry import DeployedAppsRegistry


async def reconcile_deployed_apps(db_path: str, http_get: Optional[Callable] = None) -> int:
    """Re-check every ``live`` row across ALL tenants. Returns the count flipped
    to ``failed``."""
    if http_get is None:
        from tools.hf_deploy.broker import default_http_get
        http_get = default_http_get

    registry = DeployedAppsRegistry(db_path)
    flipped = 0
    for row in registry.list_live_all():
        url = f"{row.get('public_url') or ''}{row.get('health_path') or ''}"
        try:
            status = http_get(url, 10.0)
            if asyncio.iscoroutine(status):
                status = await status
            healthy = 200 <= int(status) < 300
        except Exception:
            continue  # fail-open: an unreachable check must never mutate a row
        if not healthy:
            registry.record_failed(row["app_name"], row["user_id"])
            flipped += 1
    return flipped


__all__ = ["reconcile_deployed_apps"]
