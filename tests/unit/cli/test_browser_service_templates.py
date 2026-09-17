"""polyrob browser (049): the deployment files ARE the CLI's rendered templates,
and the templates carry the boundary — sandbox ON, userns granted, egress
chain dropping loopback/RFC1918/metadata for the browser UID, loopback-only CDP.
"""
import os
from pathlib import Path

import pytest

from cli.commands import browser as b

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name,path", [
    ("unit", "deployment/polyrob-browser.service"),
    ("server-unit", "deployment/polyrob-browser-server.service"),
    ("egress-unit", "deployment/polyrob-browser-egress.service"),
    ("egress-script", "deployment/hardening/polyrob-browser-egress.sh"),
    ("apparmor", "deployment/hardening/apparmor/polyrob-browser"),
])
def test_deployment_file_is_the_rendered_template(name, path):
    file = REPO / path
    if not file.exists():
        pytest.skip(f"{path} not shipped in this export")
    assert file.read_text(encoding="utf-8") == b.TEMPLATES[name](), (
        f"{path} drifted from cli/commands/browser.py — regenerate with "
        f"`polyrob browser render {name}`")


def test_unit_keeps_the_sandbox_and_stays_on_loopback():
    unit = b.render_unit()
    assert "--no-sandbox" not in unit
    assert "--remote-debugging-address=127.0.0.1" in unit
    assert "Requires=polyrob-browser-egress.service" in unit
    assert "User=polyrob-browser" in unit and "NoNewPrivileges=true" in unit
    assert "MemoryMax=" in unit
    assert "SEED" not in unit and "EnvironmentFile" not in unit


def test_apparmor_profile_grants_userns_for_the_binaries_only():
    prof = b.render_apparmor_profile()
    assert "userns," in prof
    assert ("profile polyrob-browser /opt/polyrob-browser/{,**/}{chrome,headless_shell} "
            "flags=(unconfined)") in prof


def test_server_unit_keeps_the_token_out_of_the_unit_and_binds_privately():
    """Phase 5: the Playwright server on a second host."""
    unit = b.render_server_unit(python="/srv/venv/bin/python")
    assert "EnvironmentFile=/etc/polyrob/browser-server.env" in unit
    assert "--path /${BROWSER_SERVER_TOKEN}" in unit and "--host ${BROWSER_SERVER_HOST}" in unit
    assert "/srv/venv/bin/python -m playwright run-server" in unit
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/polyrob-browser" in unit
    assert "--no-sandbox" not in unit and "--unsafe" not in unit
    assert "Requires=polyrob-browser-egress.service" in unit


def test_server_mode_refuses_a_public_bind_and_needs_a_listen_address():
    from click.testing import CliRunner
    r = CliRunner().invoke(b.browser, ["install", "--mode", "server", "--dry-run"])
    assert r.exit_code != 0 and "--listen" in r.output
    r = CliRunner().invoke(b.browser, ["install", "--mode", "server", "--listen", "0.0.0.0", "--dry-run"])
    assert r.exit_code != 0 and "PRIVATE" in r.output
    r = CliRunner().invoke(b.browser, ["install", "--mode", "server", "--listen", "10.0.0.5", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "--- server-unit" in r.output and "--- apparmor" in r.output


def test_egress_chain_drops_loopback_rfc1918_and_metadata_for_the_browser_uid():
    script = b.render_egress_script()
    assert 'meta skuid != "$USER_NAME" accept' in script
    assert "ct state established,related accept" in script
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert cidr in script, cidr
    assert "udp dport 53 accept" in script and "/etc/resolv.conf" in script  # loopback resolvers still work
    assert "set -euo pipefail" in script


def test_installed_revision_parses_the_symlink(tmp_path):
    target = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
    target.parent.mkdir(parents=True)
    target.write_text("")
    (tmp_path / "chrome").symlink_to(target.relative_to(tmp_path))
    assert b.installed_chromium_revision(tmp_path) == "1234"
    assert b.installed_chromium_revision(tmp_path / "nope") == ""


def test_render_command_matches_functions():
    from click.testing import CliRunner
    out = CliRunner().invoke(b.browser, ["render", "unit"]).output
    assert out == b.render_unit()


def test_install_dry_run_writes_nothing(tmp_path, monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setattr(b, "UNIT_PATH", tmp_path / "unit")
    res = CliRunner().invoke(b.browser, ["install", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "--- unit" in res.output and not (tmp_path / "unit").exists()


def test_group_is_registered():
    from cli.polyrob import _LAZY_SUBCOMMANDS
    assert _LAZY_SUBCOMMANDS["browser"] == "cli.commands.browser:browser"
