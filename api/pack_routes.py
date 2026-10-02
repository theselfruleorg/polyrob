"""Mount the enabled packs' API routers (067 P2).

Each router mounts under ``/api/packs/<pack id>`` so a pack can never shadow a
core route. A router that fails to resolve or mount is logged and recorded on
its pack (``core.packs.state``), so ``polyrob pack doctor`` and the ``packs``
status section name it — never silently absent.
"""
from fastapi import Depends, Request, Response

from core.packs import state
from core.packs.loader import api_routers, load_packs
from core.packs.spec import resolve_ref


#: Core-owned (never pack-declared) legacy mounts: 1.2.0 moved the venue routes
#: from ``/api/{polymarket,hyperliquid}`` to ``/api/packs/markets/...``. The old
#: prefix stays for ONE release, deprecated and out of the schema; removed in 1.3.0.
LEGACY_PREFIXES = {"markets": "/api"}
LEGACY_REMOVED_IN = "1.3.0"


def _legacy_headers(pack_id: str):
    def _mark(request: Request, response: Response) -> None:
        new = f"/api/packs/{pack_id}" + request.url.path[len(LEGACY_PREFIXES[pack_id]):]
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = f'<{new}>; rel="successor-version"'
        response.headers["X-Polyrob-Removed-In"] = LEGACY_REMOVED_IN
    return _mark


def _first_party(pack_id: str) -> bool:
    rec = state.record(pack_id)
    return rec is not None and rec.tier == "first-party"


def mount_pack_routers(app, logger) -> int:
    """Include every loaded pack's routers; returns how many mounted."""
    load_packs()
    mounted = 0
    for pack_id, ref in api_routers():
        try:
            router = resolve_ref(ref)
            app.include_router(router, prefix=f"/api/packs/{pack_id}",
                               tags=[f"pack:{pack_id}"])
            legacy = LEGACY_PREFIXES.get(pack_id)
            if legacy is not None and _first_party(pack_id):
                app.include_router(router, prefix=legacy, deprecated=True,
                                   include_in_schema=False,
                                   dependencies=[Depends(_legacy_headers(pack_id))])
            mounted += 1
        except Exception as exc:  # noqa: BLE001 — one pack's router never stops the app
            message = f"api router {ref!r} not mounted: {type(exc).__name__}: {exc}"
            logger.error("pack %s: %s", pack_id, message)
            rec = state.record(pack_id)
            if rec is not None:
                rec.errors.append(message)
    return mounted
