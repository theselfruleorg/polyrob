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


def test_dead_target_store_registered_and_attached_to_router(tmp_path, monkeypatch):
    """T1.5 Task 4: install_surface_bus must build a DeadTargetStore, register it
    as the ``dead_targets`` service (the seam ``core/surfaces/dispatcher.py::
    route_inbound`` reads for revive-on-inbound), and attach the SAME instance to
    the router (its direct-send path gate)."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    installed = _bootstrap().install_surface_bus(c, str(tmp_path / "surfaces.db"))
    assert installed is True

    from core.surfaces.dead_targets import DeadTargetStore
    dt = c.get_service("dead_targets")
    assert isinstance(dt, DeadTargetStore)

    router = c.get_service("message_router")
    assert router._dt is dt


def test_dead_target_store_passed_to_dispatcher(tmp_path, monkeypatch):
    """With the durable outbound queue on, the constructed OutboundDispatcher must
    receive the SAME DeadTargetStore instance registered on the container."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.setenv("OUTBOUND_QUEUE_ENABLED", "true")
    c = _FakeContainer()
    installed = _bootstrap().install_surface_bus(c, str(tmp_path / "surfaces.db"))
    assert installed is True

    dt = c.get_service("dead_targets")
    dispatcher = c.get_service("outbound_dispatcher")
    assert dispatcher is not None
    assert dispatcher._dt is dt


def test_dead_target_store_survives_idempotent_reinstall(tmp_path, monkeypatch):
    """A second install_surface_bus call (reusing the existing router) must not drop
    the dead-target wiring."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    c = _FakeContainer()
    bs = _bootstrap()
    bs.install_surface_bus(c, str(tmp_path / "surfaces.db"))
    dt_first = c.get_service("dead_targets")
    bs.install_surface_bus(c, str(tmp_path / "surfaces.db"))
    router = c.get_service("message_router")
    assert c.get_service("dead_targets") is dt_first
    assert router._dt is dt_first


def test_task_agent_lite_does_not_hardcode_surfaces_db_path():
    """SB-04 regression guard: the TaskAgent._initialize install must NOT pass a
    hardcoded 'data/surfaces.db' (that made the idempotent bus pin the outbound
    allowlist DB to ./data while `polyrob owner allow` writes <data_home>/surfaces.db).
    It must call install_surface_bus with no explicit path so the config-aware default
    resolves to container.config.data_dir."""
    import inspect
    import agents.task_agent_lite as tal

    src = inspect.getsource(tal)
    assert 'os.path.join("data", "surfaces.db")' not in src
    assert "install_surface_bus(self.container)" in src
