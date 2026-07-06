"""SurfaceRegistry + DI registration (mirrors register_goal_tool/register_cronjob_tool).

No entry-point/plugin infra: surfaces are registered by an explicit
register_surface(container, surface) call in the lifespan, each gated by its own
flag. is_surface_enabled is a cheap predicate that never constructs the surface
(Hermes env-enablement idea, plain-registry implementation).
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
    reg = container.get_service("surface_registry")
    if reg is None:
        reg = SurfaceRegistry()
        container.register_service("surface_registry", reg)
    reg.add(surface)
    router = container.get_service("message_router")
    if router is not None and hasattr(router, "subscribe"):
        router.subscribe(surface.surface_id, surface)


def is_surface_enabled(container, surface_id: str) -> bool:
    reg = container.get_service("surface_registry")
    return bool(reg and reg.get(surface_id) is not None)
