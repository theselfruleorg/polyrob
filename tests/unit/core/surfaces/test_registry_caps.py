from core.surfaces.registry import SurfaceRegistry
from core.surfaces.envelopes import SurfaceCapabilities
from core.surfaces.surface import SurfaceConfigSchema, SurfaceConfigField


class _FakeSurface:
    surface_id = "fake"
    capabilities = SurfaceCapabilities(supports_edit=True, max_message_bytes=100)

    @classmethod
    def config_schema(cls):
        return SurfaceConfigSchema(fields=[SurfaceConfigField(name="token", secret=True)])


def test_registry_returns_capabilities():
    r = SurfaceRegistry(); r.add(_FakeSurface())
    caps = r.capabilities("fake")
    assert caps is not None and caps.supports_edit is True


def test_registry_returns_config_schema():
    r = SurfaceRegistry(); r.add(_FakeSurface())
    sch = r.config_schema("fake")
    assert sch is not None and sch.fields[0].name == "token" and sch.fields[0].secret is True


def test_missing_surface_returns_none():
    r = SurfaceRegistry()
    assert r.capabilities("nope") is None and r.config_schema("nope") is None
