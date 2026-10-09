"""Mount the enabled packs' API routers (067 P2).

Each router mounts under ``/api/packs/<pack id>`` so a pack can never shadow a
core route. A router that fails to resolve or mount is logged and recorded on
its pack (``core.packs.state``), so ``polyrob pack doctor`` and the ``packs``
status section name it — never silently absent.
"""

from core.packs import state
from core.packs.loader import api_routers, load_packs
from core.packs.spec import resolve_ref


def mount_pack_routers(app, logger) -> int:
    """Include every loaded pack's routers; returns how many mounted."""
    load_packs()
    mounted = 0
    for pack_id, ref in api_routers():
        try:
            router = resolve_ref(ref)
            app.include_router(router, prefix=f"/api/packs/{pack_id}",
                               tags=[f"pack:{pack_id}"])
            mounted += 1
        except Exception as exc:  # noqa: BLE001 — one pack's router never stops the app
            message = f"api router {ref!r} not mounted: {type(exc).__name__}: {exc}"
            logger.error("pack %s: %s", pack_id, message)
            rec = state.record(pack_id)
            if rec is not None:
                rec.errors.append(message)
    return mounted
