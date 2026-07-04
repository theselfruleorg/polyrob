"""P1b-0: install_surface_bus constructs + registers the outbound bus.

The bus (SessionChatRegistry + MessageRouter) is what every P1a mirror and
cron/delivery.py expects on the container. Before this, container.get_service(
"message_router") was always None (the bus was never built), so the mirrors were
inert and cron Telegram delivery was silently blocked. Flag-gated so flag-OFF
means the services never exist -> mirrors stay no-op -> byte-identical to today.
"""
import importlib


class _FakeContainer:
    def __init__(self):
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, instance, **kwargs):
        self._svc[name] = instance


def _bootstrap():
    return importlib.import_module("core.surfaces.bootstrap")


def test_flag_off_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SINGULAR_CHAT_ENABLED", raising=False)
    c = _FakeContainer()
    installed = _bootstrap().install_surface_bus(c, str(tmp_path / "surfaces.db"))
    assert installed is False
    assert c.get_service("message_router") is None
    assert c.get_service("session_chat_registry") is None


def test_flag_on_installs_router_and_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    installed = _bootstrap().install_surface_bus(c, str(tmp_path / "surfaces.db"))
    assert installed is True
    from core.surfaces.message_router import MessageRouter
    from core.surfaces.session_chat_registry import SessionChatRegistry
    assert isinstance(c.get_service("message_router"), MessageRouter)
    assert isinstance(c.get_service("session_chat_registry"), SessionChatRegistry)


def test_default_db_path_follows_container_data_dir(tmp_path, monkeypatch):
    # With no explicit db_path, the bus DB must follow container.config.data_dir
    # (POLYROB_DATA_DIR isolation) instead of a hardcoded ./data.
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    c.config = type("Cfg", (), {"data_dir": str(tmp_path)})()
    installed = _bootstrap().install_surface_bus(c)  # no db_path
    assert installed is True
    assert (tmp_path / "surfaces.db").exists()


def test_idempotent_reuses_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    bs = _bootstrap()
    bs.install_surface_bus(c, str(tmp_path / "surfaces.db"))
    router_first = c.get_service("message_router")
    # second call must NOT clobber the live router (would drop subscriptions)
    installed_again = bs.install_surface_bus(c, str(tmp_path / "surfaces.db"))
    assert installed_again is True
    assert c.get_service("message_router") is router_first


def test_router_wired_to_registry(tmp_path, monkeypatch):
    """The installed router resolves session keys via the installed registry."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    _bootstrap().install_surface_bus(c, str(tmp_path / "surfaces.db"))
    reg = c.get_service("session_chat_registry")
    router = c.get_service("message_router")
    reg.bind("k1", "sess_1", "u1", "telegram", "555")
    # router holds the same registry instance -> resolve works
    assert router._registry.resolve("k1")["session_id"] == "sess_1"
