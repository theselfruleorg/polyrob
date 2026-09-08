"""SurfaceRegistry + DI registration (mirrors register_goal_tool/register_cronjob_tool).

No entry-point/plugin infra: surfaces are registered by an explicit
register_surface(container, surface) call in the lifespan, each gated by its own
flag. is_surface_enabled is a cheap predicate that never constructs the surface
(a plain-registry, env-flag-gated implementation).
"""


class SurfaceRegistry:
    def __init__(self) -> None:
        self._surfaces: dict[str, object] = {}

    def add(self, surface) -> None:
        self._surfaces[surface.surface_id] = surface

    def get(self, surface_id: str):
        return self._surfaces.get(surface_id)

    def all(self) -> list:
        return list(self._surfaces.values())

    def enabled_ids(self) -> list:
        return list(self._surfaces.keys())

    def capabilities(self, surface_id: str):
        s = self._surfaces.get(surface_id)
        return getattr(s, "capabilities", None) if s is not None else None

    def config_schema(self, surface_id: str):
        s = self._surfaces.get(surface_id)
        if s is None:
            return None
        fn = getattr(s, "config_schema", None)
        return fn() if callable(fn) else None


def register_surface(container, surface) -> None:
    """Register + subscribe a surface, ENFORCING the contract (030 WS-B2).

    A malformed surface used to fail silently much later (no capabilities ->
    ``surface_profile()`` None -> the agent never told the surface's shape;
    no send -> messages dropped). Registration-time errors are startup errors.
    """
    sid = getattr(surface, "surface_id", "") or ""
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("register_surface: surface has no surface_id")
    if getattr(surface, "capabilities", None) is None:
        raise ValueError(f"register_surface: surface '{sid}' declares no capabilities")
    if not callable(getattr(surface, "send", None)):
        raise ValueError(f"register_surface: surface '{sid}' has no send()")
    reg = container.get_service("surface_registry")
    if reg is None:
        reg = SurfaceRegistry()
        register = getattr(container, "register_service", None)
        if callable(register):
            register("surface_registry", reg)
    reg.add(surface)
    router = container.get_service("message_router")
    if router is not None and hasattr(router, "subscribe"):
        router.subscribe(surface.surface_id, surface)


def is_surface_enabled(container, surface_id: str) -> bool:
    reg = container.get_service("surface_registry")
    return bool(reg and reg.get(surface_id) is not None)
