"""`polyrob apps install` — the serving side rendered for the detected layout."""
import os
import pwd
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands import apps_install as ai



def _directives(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _user_layout(tmp_path):
    home = tmp_path / ".polyrob"
    return ai.Layout("user", "polyrob", "polyrob", str(home), str(home / "data"),
                     str(home / ".env"), str(tmp_path),
                     f"{home}/venv/bin/python -I -m cli.polyrob apps supervise")


def test_user_render_keeps_the_seed_out(tmp_path):
    layout = _user_layout(tmp_path)
    text = ai.render_unit(layout, nginx_dir="/etc/nginx/apps.d")
    d = _directives(text)
    assert "User=root" in d and "Group=polyrob" in d
    assert f'Environment="POLYROB_HOME={layout.polyrob_home}"' in d
    assert f'Environment="POLYROB_DATA_DIR={layout.data_dir}"' in d
    assert f"EnvironmentFile=-{ai.APPS_ENV_PATH}" in d
    assert not any("polyrob.env" in ln for ln in d)
    assert f"ReadWritePaths=-/etc/nginx/apps.d {layout.data_dir}" in d
    inacc = [ln for ln in d if ln.startswith("InaccessiblePaths=")][0]
    assert f"-{layout.polyrob_home}/.env" in inacc.split("=", 1)[1].split()
    assert "ProtectHome=read-only" in d and "/opt/polyrob" not in text


def test_apps_env_copies_only_app_flags():
    env = {"APP_SERVICE_MAX_LIVE": "2", "CODE_EXEC_DEV_IMAGE": "img:1",
           "WALLET_SEED_PHRASE": "abandon " * 12, "OPENAI_API_KEY": "sk-x",
           "APP_SERVICE_BASE_DOMAIN": "old.example.com"}
    text = ai.render_apps_env(env, base_domain="apps.example.com")
    assert "AGENT_BUILDER_MODE=ship" in text
    assert "APP_SERVICE_BASE_DOMAIN=apps.example.com" in text and "old.example.com" not in text
    assert "APP_SERVICE_MAX_LIVE=2" in text and "CODE_EXEC_DEV_IMAGE=img:1" in text
    assert "abandon" not in text and "sk-x" not in text


def test_root_detects_the_user_unit(tmp_path):
    me = pwd.getpwuid(os.getuid())
    unit = tmp_path / "polyrob.service"
    unit.write_text('[Service]\nEnvironment="POLYROB_HOME=/srv/x/.polyrob"\n'
                    "ExecStart=/srv/x/.polyrob/venv/bin/python -I -m cli.polyrob gateway\n")
    layout = ai.detect_layout(is_root=True, system_present=False,
                              service_users=lambda: [(me.pw_name, me.pw_uid, unit)])
    assert layout.kind == "user" and layout.agent_user == me.pw_name
    assert layout.polyrob_home == "/srv/x/.polyrob" and layout.data_dir == "/srv/x/.polyrob/data"
    assert layout.agent_env_file == "/srv/x/.polyrob/.env"
    assert layout.exec_start.startswith("/srv/x/.polyrob/venv/bin/python -I -m cli.polyrob apps supervise")
    import click
    with pytest.raises(click.ClickException):
        ai.detect_layout(is_root=True, system_present=False, service_users=lambda: [])


def _plan(tmp_path, *, cert_ok=True, image_missing=False, publish="/var/www/polyrob-publish"):
    return ai.build_plan(_user_layout(tmp_path), base_domain="apps.example.com",
                         cert_dir="/etc/letsencrypt/live/apps.example.com",
                         nginx_dir="/etc/nginx/apps.d", publish_root=publish, image="img:1",
                         image_missing=image_missing, cert_ok=cert_ok, environ={})


def test_plan_order_and_static_host(tmp_path):
    steps = _plan(tmp_path, image_missing=True)
    targets = [s.path or " ".join(s.argv) for s in steps]
    assert targets[0] == ai.UNIT_PATH and targets[1] == ai.APPS_ENV_PATH
    assert "install -d -o polyrob -g polyrob -m 0755 /var/www/polyrob-publish" in targets
    assert ai.STATIC_CONF_PATH in targets and "docker pull img:1" in targets
    assert targets.index("nginx -t") < targets.index("systemctl reload nginx")
    assert targets[-1] == "systemctl enable --now polyrob-apps.service"
    static = [s for s in steps if s.path == ai.STATIC_CONF_PATH][0].content
    assert "root /var/www/polyrob-publish;" in static
    # no cert yet: the static host would fail `nginx -t`, so it is not written
    assert ai.STATIC_CONF_PATH not in [s.path for s in _plan(tmp_path, cert_ok=False)]
    assert not any("publish" in (s.path + " ".join(s.argv)) for s in _plan(tmp_path, publish=None))


def test_execute_writes_and_stops_on_failure(tmp_path):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "boom"

    def runner(argv, **kw):
        calls.append(argv)
        return R(1 if argv[0] == "nginx" else 0)

    steps = [ai.Step("f", path=str(tmp_path / "etc" / "a.conf"), content="x", mode=0o640),
             ai.Step("t", argv=["nginx", "-t"]), ai.Step("never", argv=["systemctl", "x"])]
    out = []
    assert ai.execute(steps, echo=out.append, runner=runner) == 1
    assert (tmp_path / "etc" / "a.conf").read_text() == "x"
    assert oct((tmp_path / "etc" / "a.conf").stat().st_mode & 0o777) == "0o640"
    assert calls == [["nginx", "-t"]] and "FAILED nginx -t: boom" in out[-1]


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "detect_layout", lambda **kw: _user_layout(tmp_path))
    monkeypatch.setattr(ai, "_docker_image_present", lambda image, runner=None: False)
    monkeypatch.setattr(ai.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(tmp_path / "nocert"))
    monkeypatch.delenv("APP_SERVICE_BASE_DOMAIN", raising=False)


def test_cli_dry_run_changes_nothing(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    res = CliRunner().invoke(ai.install_cmd, ["--domain", "apps.example.com", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "layout: user" in res.output and "cannot read" in res.output
    assert "no certificate at" in res.output and "certbot-dns-cloudflare" in res.output
    assert "is not pulled" in res.output and f"--- {ai.UNIT_PATH}" in res.output
    assert "PUBLISH_BASE_URL=https://static.apps.example.com" in res.output
    assert "PUBLISH_ROOT=/var/www/polyrob-publish" in res.output
    assert not (tmp_path / ".polyrob" / "apps-install").exists()


def test_cli_non_root_stages_and_prints_sudo(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    res = CliRunner().invoke(ai.install_cmd, ["--domain", "apps.example.com"])
    assert res.exit_code == 0, res.output
    stage = tmp_path / ".polyrob" / "apps-install"
    assert (stage / "polyrob-apps.service").is_file() and (stage / "apps.env").is_file()
    assert f"sudo install -D -m 0644 {stage}/polyrob-apps.service {ai.UNIT_PATH}" in res.output
    assert "sudo systemctl enable --now polyrob-apps.service" in res.output


def test_cli_needs_a_domain(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    res = CliRunner().invoke(ai.install_cmd, ["--dry-run"])
    assert res.exit_code != 0 and "no base domain" in res.output


def test_registered_under_apps():
    from cli.commands.apps import apps
    assert "install" in apps.commands
