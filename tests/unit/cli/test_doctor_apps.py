"""`polyrob doctor`: durable apps + static publish readiness (doctor_apps)."""
import os

import pytest

from cli.commands import doctor_apps as da

_ENV = ("AGENT_BUILDER_MODE", "APP_SERVICE_BASE_DOMAIN", "APP_SERVICE_CERT_DIR",
        "APP_SERVICE_NGINX_CONF_DIR", "APP_SERVICE_IMAGE", "CODE_EXEC_DEV_IMAGE",
        "PUBLISH_ENABLED", "PUBLISH_ROOT", "PUBLISH_BASE_URL", "POLYROB_OVERRIDES")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(da, "_image_present", lambda image: True)


def test_silent_when_nothing_is_in_use():
    assert da.apps_publish_lines({}) == []


def test_ship_reports_absent_cert_include_dir_and_image(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("APP_SERVICE_NGINX_CONF_DIR", str(tmp_path / "apps.d"))
    monkeypatch.setattr(da, "_image_present", lambda image: False)
    text = "\n".join(da.apps_publish_lines({}))
    assert "apps cert: ! no certificate at" in text
    assert f"apps nginx: ! include dir {tmp_path / 'apps.d'} is missing" in text
    assert "apps image: ! " in text and "docker pull" in text
    assert "polyrob apps install" in text


def test_ship_all_ready(monkeypatch, tmp_path):
    (tmp_path / "cert").mkdir()
    (tmp_path / "cert" / "fullchain.pem").write_text("x")
    (tmp_path / "apps.d").mkdir()
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "cert"))
    monkeypatch.setenv("APP_SERVICE_NGINX_CONF_DIR", str(tmp_path / "apps.d"))
    lines = [ln for ln in da.apps_publish_lines({}) if ln.startswith("apps")]
    assert len(lines) == 3 and not any("!" in ln for ln in lines), lines


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 0700 directory")
def test_cert_present_but_unreadable(monkeypatch, tmp_path):
    live = tmp_path / "live"
    (live / "d").mkdir(parents=True)
    (live / "d" / "fullchain.pem").write_text("x")
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "apps.example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(live / "d"))
    os.chmod(live, 0o600)
    try:
        text = "\n".join(da.apps_publish_lines({}))
    finally:
        os.chmod(live, 0o755)
    assert "not readable by" in text and "no certificate" not in text


def _uid_not_me():
    return os.getuid() + 4242


def test_publish_root_not_servable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    root = home / "data" / "publish"
    root.mkdir(parents=True)
    os.chmod(tmp_path, 0o755)
    os.chmod(home, 0o750)  # a user home: nginx is "other" here
    monkeypatch.setattr(da, "_ancestors", lambda p: [tmp_path, home, home / "data", root])
    monkeypatch.setenv("PUBLISH_ENABLED", "true")
    monkeypatch.setenv("PUBLISH_ROOT", str(root))
    monkeypatch.setattr(da, "_nginx_identity", lambda: ("www-data", _uid_not_me(), set()))
    text = "\n".join(da.apps_publish_lines({}))
    assert f"publish root: ! {root} is not servable: www-data cannot traverse {home} (0750)" in text
    assert "PUBLISH_ROOT=/var/www/polyrob-publish" in text
    assert "publish url: ! PUBLISH_BASE_URL is unset" in text


def test_publish_root_servable(monkeypatch, tmp_path):
    root = tmp_path / "pub"
    root.mkdir()
    for p in (tmp_path, root):
        os.chmod(p, 0o755)
    monkeypatch.setenv("PUBLISH_ENABLED", "true")
    monkeypatch.setenv("PUBLISH_ROOT", str(root))
    monkeypatch.setenv("PUBLISH_BASE_URL", "https://static.apps.example.test")
    monkeypatch.setattr(da, "_nginx_identity", lambda: ("www-data", _uid_not_me(), set()))
    # the parents of tmp_path on a dev box may be private; judge only from tmp_path down
    monkeypatch.setattr(da, "_ancestors", lambda p: [tmp_path, root])
    lines = da.apps_publish_lines({})
    assert lines == [f"publish root: ok ({root}, readable by www-data)",
                     "publish url: https://static.apps.example.test"]


def test_registered_in_doctor_host():
    from cli.commands.doctor_host import _checkers
    assert da.apps_publish_lines in _checkers()
