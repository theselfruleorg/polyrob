"""032 durable app service — the shared contracts every later task builds on:
capability row, event kinds, DB manifest, the `apps` pause scope, flag rows."""


def test_capability_row():
    from core.tool_capabilities import TOOL_CAPABILITIES
    assert TOOL_CAPABILITIES["app_service"] == frozenset({"high_impact", "delegate_blocked"})


def test_event_kinds():
    from core import event_kinds as k
    assert {k.APP_REQUESTED, k.APP_APPROVED, k.APP_LIVE, k.APP_FAILED, k.APP_STOPPED} == {
        "app_requested", "app_approved", "app_live", "app_failed", "app_stopped"}


def test_db_manifest():
    from core.db_manifest import SIDECAR_DB_NAMES
    assert "app_services.db" in SIDECAR_DB_NAMES


def test_pause_scope_and_kinds():
    from core.autonomy_control import KIND_SCOPES, SCOPES
    assert "apps" in SCOPES
    assert KIND_SCOPES["app_deploy"] == ("all", "apps")
    assert KIND_SCOPES["app_serve"] == ("all", "apps")


def test_pause_apps_denies_app_kinds_only(tmp_path, monkeypatch):
    from core import autonomy_control as ac
    # ONE base only (mirrors tests/unit/core/test_autonomy_control.py::home): the
    # record is written to every probed base, so an un-isolated call would pause
    # the developer's real data home.
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    ac.pause(str(tmp_path), scopes=("apps",), via="test")
    assert not ac.allows("app_deploy", str(tmp_path)).allowed
    assert not ac.allows("app_serve", str(tmp_path)).allowed
    assert ac.allows("dispatch", str(tmp_path)).allowed


def test_flag_rows_cataloged():
    from core.flags import REGISTRY
    for n in ("APP_SERVICE_ENABLED", "APP_SERVICE_ALLOW_PUBLIC", "APP_SERVICE_BASE_DOMAIN",
              "APP_SERVICE_MAX_LIVE", "APP_SERVICE_DAILY_MAX", "APP_SERVICE_MIN_INTERVAL_SEC",
              "APP_SERVICE_MEMORY_MB", "APP_SERVICE_CPUS", "APP_SERVICE_PIDS",
              "APP_SERVICE_SNAPSHOT_MAX_MB", "APP_SERVICE_EGRESS_DEFAULT", "APP_SERVICE_PORT_RANGE",
              "APP_SERVICE_TICK_SEC", "APP_SERVICE_HEALTH_TIMEOUT_SEC", "APP_SERVICE_IMAGE",
              "APP_SERVICE_RETAIN_DAYS", "APP_SERVICE_CERT_DIR", "APP_SERVICE_NGINX_CONF_DIR",
              "APP_SERVICES_DB_PATH", "AGENT_BUILDER_MODE"):
        assert n in REGISTRY, n
