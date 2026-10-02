"""Pack console routers — ``PackSpec.console_routers`` on the console.

The console (``webview/server.py``) is the process that serves the public HTTPS
host; the API app (``api/app.py``, where ``PackSpec.api_routers`` mount) does not
run on a deployed box. A pack that needs a route a BROWSER reaches — an OAuth
callback, an owner form — declares it here, and it mounts under the same prefix
as its API routers: ``/api/packs/<pack id>``, so a pack can never shadow a core
route.

**Every pack console route is owner-only**, by three layers:

1. the console's auth middleware (``server.auth_middleware``): own_ops and
   multitenant require a valid session token before the request reaches a
   route; the ``local`` posture is the loopback owner;
2. :func:`pack_owner_guard`, mounted on EVERY pack console router: the
   authenticated principal must be the instance owner (own_ops: the bound
   owner; multitenant: an admin by the ONE role predicate). A tenant is
   authenticated and still not the owner of this instance's accounts;
3. ``webgate.MUTATION_DEPS`` (read-only + same-origin CSRF) on every route, so
   a POST a pack adds later is refused on a ``WEBVIEW_READ_ONLY`` console and
   from a cross-origin page.

**The public carve-out** is the ONE exception, and it is narrow by
construction: an EXACT path (never a prefix) that a FIRST-PARTY pack lists in
``pack.toml`` ``[console] public_paths`` (validated in phase 1 by
``core.packs.manifest``), reachable only with ``GET``/``HEAD``. It exists for a
redirect a third party sends the owner's browser to — X's OAuth callback lands
on a fresh tab that may hold no console session. Such a route must carry its
own proof (the X callback: a single-use, owner-bound, 10-minute ``state``).
Every public path is logged at mount, and a request to one is logged.

⚠️ Read-only posture: a public GET that WRITES (the OAuth callback stores one
token record) is a deliberate exception to ``WEBVIEW_READ_ONLY``, which refuses
mutating METHODS only. It writes the agent's credential store, never console
or agent state, and only with a proof the owner's own verb minted.
"""
from __future__ import annotations

import logging
from typing import FrozenSet, List

from fastapi import Depends, HTTPException, Request

from webview import webgate

logger = logging.getLogger(__name__)

PREFIX = "/api/packs"

#: Methods a public pack path answers without the owner's session.
PUBLIC_METHODS = frozenset({"GET", "HEAD"})

#: Exact full paths (``/api/packs/<id>/<path>``) mounted as public. Filled by
#: :func:`mount_pack_console_routers` from the loaded packs only.
_PUBLIC: set = set()

#: ``"<pack>: <error>"`` for a router that failed to mount (the console doctor
#: reads ``server.UNMOUNTED_ROUTERS``; this is the pack-local copy for tests).
UNMOUNTED: List[str] = []


def public_paths() -> FrozenSet[str]:
    return frozenset(_PUBLIC)


def is_public(path: str, method: str) -> bool:
    """True for exactly a declared public path, requested with GET/HEAD."""
    return method.upper() in PUBLIC_METHODS and path in _PUBLIC


def _is_instance_owner(request: Request) -> bool:
    if not webgate.requires_owner_login():
        return True                                   # local: the loopback owner
    state = getattr(request, "state", None)
    if not getattr(state, "authenticated", False):
        return False
    if webgate.is_own_ops():
        uid = str(getattr(state, "user_id", "") or "")
        return bool(uid) and uid == str(webgate.local_owner_id())
    return webgate.request_is_admin(request)          # multitenant: the operator


async def pack_owner_guard(request: Request) -> None:
    """Refuse a pack console request from anyone but the instance owner — except
    a GET/HEAD on a declared public path."""
    method = request.scope.get("method", "")
    if is_public(request.url.path, method):
        logger.info("pack console: public %s %s", method, request.url.path)
        return
    if not _is_instance_owner(request):
        # Machine-shaped (the copy-layer ratchet reads this module).
        raise HTTPException(status_code=403, detail="pack_console_owner_only")


def mount_pack_console_routers(app, record_failure=None) -> int:
    """Mount every loaded pack's console routers under ``/api/packs/<id>``;
    returns how many mounted. A failure is logged, recorded on its pack
    (``core.packs.state``) and passed to ``record_failure(name, exc)`` — it
    never stops the console."""
    from core.packs import state
    from core.packs.loader import console_public_paths, console_routers, load_packs
    from core.packs.spec import resolve_ref

    load_packs()
    deps = [Depends(pack_owner_guard), *webgate.MUTATION_DEPS]
    served: set = set()
    count = 0
    for pack_id, ref in console_routers():
        try:
            router = resolve_ref(ref)
            app.include_router(router, prefix=f"{PREFIX}/{pack_id}",
                               tags=[f"pack:{pack_id}"], dependencies=deps)
            # The router's OWN route paths (FastAPI may include a router lazily,
            # so the app's route list is not the place to look).
            served |= {f"{PREFIX}/{pack_id}{getattr(r, 'path', '')}"
                       for r in getattr(router, "routes", ())}
            count += 1
        except Exception as exc:  # noqa: BLE001 — one pack's router never stops the console
            message = f"console router {ref!r} not mounted: {type(exc).__name__}: {exc}"
            logger.error("pack %s: %s", pack_id, message)
            UNMOUNTED.append(f"{pack_id}: {message}")
            rec = state.record(pack_id)
            if rec is not None:
                rec.errors.append(message)
            if record_failure is not None:
                record_failure(f"pack {pack_id} console", exc)
    for path in console_public_paths():
        pack_id = path[len(PREFIX) + 1:].split("/", 1)[0]
        if path not in served:
            # A declared public path no route serves opens nothing — say so.
            logger.warning("pack %s declares public console path %s but no route "
                           "serves it; ignored", pack_id, path)
            continue
        _PUBLIC.add(path)
        logger.warning("pack %s: console path %s is PUBLIC (GET, no owner session; "
                       "the route carries its own proof)", pack_id, path)
    return count


def mount_into(app, record_failure) -> None:
    """The console shell's one call (``pages_new.mount``): mount, and record a
    fault of the seam itself (never raises)."""
    try:
        mount_pack_console_routers(app, record_failure=record_failure)
    except Exception as exc:  # noqa: BLE001 — additive mount, never fatal
        logger.error("pack console routers not mounted: %s", exc)
        UNMOUNTED.append(f"pack console: {type(exc).__name__}: {exc}")
        if record_failure is not None:
            record_failure("pack console", exc)


def reset_for_tests() -> None:
    _PUBLIC.clear()
    UNMOUNTED.clear()


__all__ = ["PREFIX", "PUBLIC_METHODS", "is_public", "mount_into", "mount_pack_console_routers",
           "pack_owner_guard", "public_paths", "reset_for_tests"]
