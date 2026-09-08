"""030 WS-B2: registration is enforcement.

Five of seven transports never called ``register_surface``, so
``surface_profile()`` returned None (the agent was never told the surface's
shape), and the correspondent registry existed only on the email seat — a
telegram-only deploy DENIED every third party. Now:
- ``register_surface`` validates the contract (id, capabilities, async send)
  and refuses a malformed surface at startup instead of failing silently later;
- every harness builder registers its surface;
- ``install_surface_bus`` registers the correspondent registry centrally when
  the access model is on.
"""
import os

import pytest

from core.surfaces.envelopes import SurfaceCapabilities
from core.surfaces.registry import register_surface


class _Container:
    def __init__(self):
        self.services = {}
        self.config = type("Cfg", (), {"data_dir": None})()

    def get_service(self, name):
        return self.services.get(name)

    def register_service(self, name, svc):
        self.services[name] = svc


class _GoodSurface:
    surface_id = "testsurf"
    capabilities = SurfaceCapabilities()

    async def send(self, msg):
        return None


def test_register_surface_accepts_a_contract_conformant_surface():
    c = _Container()
    register_surface(c, _GoodSurface())
    reg = c.get_service("surface_registry")
    assert reg is not None and reg.get("testsurf") is not None


def test_register_surface_refuses_missing_id():
    class _NoId:
        surface_id = ""
        capabilities = SurfaceCapabilities()

        async def send(self, msg):
            return None

    with pytest.raises(ValueError):
        register_surface(_Container(), _NoId())


def test_register_surface_refuses_missing_capabilities():
    class _NoCaps:
        surface_id = "x1"
        capabilities = None

        async def send(self, msg):
            return None

    with pytest.raises(ValueError):
        register_surface(_Container(), _NoCaps())


def test_register_surface_refuses_missing_send():
    class _NoSend:
        surface_id = "x2"
        capabilities = SurfaceCapabilities()

    with pytest.raises(ValueError):
        register_surface(_Container(), _NoSend())


@pytest.mark.parametrize("builder,mod,sid", [
    ("build_slack_harness", "surfaces.slack.harness", "slack"),
    ("build_signal_harness", "surfaces.signal.harness", "signal"),
    ("build_discord_harness", "surfaces.discord.harness", "discord"),
])
def test_harness_builders_register_their_surface(tmp_path, builder, mod, sid):
    import importlib
    m = importlib.import_module(mod)
    fn = getattr(m, builder)
    c = _Container()
    fn(c, task_agent=object(), data_dir=str(tmp_path))
    reg = c.get_service("surface_registry")
    assert reg is not None and reg.get(sid) is not None, (
        f"{builder} must register_surface() so the agent knows {sid}'s shape")


def test_install_surface_bus_registers_correspondent_registry_when_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    from core.surfaces.bootstrap import install_surface_bus
    c = _Container()
    assert install_surface_bus(c, db_path=os.path.join(str(tmp_path), "s.db")) is True
    assert c.get_service("correspondent_registry") is not None


def test_install_surface_bus_skips_correspondent_registry_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    from core.surfaces.bootstrap import install_surface_bus
    c = _Container()
    assert install_surface_bus(c, db_path=os.path.join(str(tmp_path), "s2.db")) is True
    assert c.get_service("correspondent_registry") is None
