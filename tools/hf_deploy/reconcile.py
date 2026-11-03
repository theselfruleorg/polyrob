"""Reconcile-on-boot (proposal §3.5, 098 TS-30): re-health-check every ``live``
and ``unhealthy`` row, and every ``deploying`` row a crash left behind, so the
registry follows what the Space actually does.

* ``live`` + non-2xx -> ``unhealthy``; ``unhealthy`` + 2xx -> ``live``.
* a stale ``deploying`` row: 2xx -> ``live``, non-2xx -> ``failed``.

Every write is a CAS on the state AND the ``last_deploy`` it read, so a deploy
that finished meanwhile is never overwritten by a probe of the old Space.
Fail-open per row: a ``http_get`` that raises (network down, DNS failure) never
mutates that row — silence is not evidence of death.
"""
import asyncio
from typing import Callable, Optional

from tools.hf_deploy.registry import DeployedAppsRegistry


async def reconcile_deployed_apps(db_path: str, http_get: Optional[Callable] = None) -> int:
    """Re-check every reconcilable row across ALL tenants. Returns the count of
    rows whose status changed."""
    if http_get is None:
        from tools.hf_deploy.broker import default_http_get
        http_get = default_http_get

    registry = DeployedAppsRegistry(db_path)
    flipped = 0
    for row in registry.list_reconcilable_all():
        url = f"{row.get('public_url') or ''}{row.get('health_path') or ''}"
        if row["status"] == "deploying" and not row.get("public_url"):
            # A crashed first deploy: no URL on record to probe. Its exit is
            # ``failed``; the next deploy retakes the app (S6).
            flipped += int(bool(registry.record_failed(
                row["app_name"], row["user_id"], error="reconcile: the deploy never finished",
                from_states=("deploying",),
                where_extra="COALESCE(deploy_started_at,0)=?",
                where_params=(row.get("deploy_started_at") or 0,))))
            continue
        try:
            status = http_get(url, 10.0)
            if asyncio.iscoroutine(status):
                status = await status
            healthy = 200 <= int(status) < 300
        except Exception:
            continue  # fail-open: an unreachable check must never mutate a row
        app, uid, state = row["app_name"], row["user_id"], row["status"]
        # The CAS also pins the deploy the probe looked at.
        pin = dict(where_extra="COALESCE(last_deploy,0)=? AND COALESCE(deploy_started_at,0)=?",
                   where_params=(row.get("last_deploy") or 0,
                                 row.get("deploy_started_at") or 0))
        if healthy and state in ("unhealthy", "deploying"):
            changed = registry.record_live(
                app, uid, space_repo=row.get("space_repo") or "",
                public_url=row.get("public_url") or "",
                health_path=row.get("health_path") or "",
                workspace_digest=row.get("workspace_digest") or "",
                from_states=(state,), **pin)
        elif not healthy and state == "live":
            changed = registry.record_unhealthy(
                app, uid, error=f"reconcile: {url} did not return 2xx",
                from_states=("live",), **pin)
        elif not healthy and state == "deploying":
            changed = registry.record_failed(
                app, uid, error="reconcile: the deploy never finished",
                from_states=("deploying",), **pin)
        else:
            changed = False
        flipped += int(bool(changed))
    return flipped


__all__ = ["reconcile_deployed_apps"]
