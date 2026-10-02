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


def test_unit_hides_the_agent_data_home_and_config_from_the_browser_uid():
    """2026-09-25 server assessment M2: a renderer escape must not read the
    owner thread / sessions or plant files in the shared project."""
    unit = b.render_unit()
    assert "\nInaccessiblePaths=-/var/lib/polyrob -/etc/polyrob\n" in unit
    assert "\nUMask=0007\n" in unit


def test_unit_hides_a_custom_agent_data_home(tmp_path, monkeypatch):
    """Codex 067 follow-up #4: install renders the EFFECTIVE data home (env,
    else the agent env file) into InaccessiblePaths, not the default."""
    monkeypatch.setenv("POLYROB_DATA_DIR", "/srv/polyrob/")
    assert b.agent_data_home() == Path("/srv/polyrob")
    unit = b.render_unit(agent_data=b.agent_data_home())
    assert "\nInaccessiblePaths=-/srv/polyrob -/etc/polyrob\n" in unit
    monkeypatch.delenv("POLYROB_DATA_DIR")
    env = tmp_path / "polyrob.env"
    env.write_text('X=1\nexport POLYROB_DATA_DIR="/data/rob"\n')
    assert b.agent_data_home(env) == Path("/data/rob")
    assert b.agent_data_home(tmp_path / "missing.env") == Path("/var/lib/polyrob")
    monkeypatch.setenv("POLYROB_DATA_DIR", "/srv/my rob")
    with pytest.raises(Exception, match="not a plain absolute path"):
        b.agent_data_home()


def test_install_dry_run_renders_the_effective_data_home(monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setenv("POLYROB_DATA_DIR", "/srv/polyrob")
    res = CliRunner().invoke(b.browser, ["install", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "InaccessiblePaths=-/srv/polyrob -/etc/polyrob" in res.output


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


def test_cdp_port_is_closed_to_every_local_uid_but_root_browser_and_agent():
    """2026-09-25 server assessment M1: CDP has no auth, so the chain rejects a
    loopback connect to the CDP port from any UID but root, the browser and the
    named clients (resolved by NAME at run time, never a hard-coded number),
    BEFORE the non-browser accept."""
    script = b.render_egress_script(port=9333)
    assert 'CDP_PORT="${POLYROB_BROWSER_CDP_PORT:-9333}"' in script
    assert 'CDP_CLIENT_NAMES="${POLYROB_BROWSER_CDP_CLIENTS:-polyrob-agent}"' in script
    assert 'id -u "$_name"' in script
    assert 'CDP_ALLOW="0, $(id -u "$USER_NAME")${CDP_UIDS:+, $CDP_UIDS}"' in script
    v4 = "ip daddr 127.0.0.0/8 tcp dport $CDP_PORT meta skuid != { $CDP_ALLOW } counter reject"
    v6 = "ip6 daddr ::1 tcp dport $CDP_PORT meta skuid != { $CDP_ALLOW } counter reject"
    assert v4 in script and v6 in script
    assert script.index("${CDP_RULES}") < script.index('meta skuid != "$USER_NAME" accept')
    import re
    assert not re.search(r"skuid != \{ ?\d", script)  # no hard-coded UID
    assert "9333" in b.render_unit(port=9333)


def _run_egress(tmp_path, users):
    """Run the rendered egress script with stub ``id``/``nft``; the nft ruleset."""
    import shutil
    import subprocess
    if not shutil.which("bash"):
        pytest.skip("no bash")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    table = " ".join(f"{k}) echo {v};;" for k, v in users.items())
    (bin_ / "id").write_text(f'#!/bin/sh\ncase "$2" in {table} *) exit 1;; esac\n')
    out = tmp_path / "ruleset"
    (bin_ / "nft").write_text(f"#!/bin/sh\ncat > {out}\n")
    for f in ("id", "nft"):
        (bin_ / f).chmod(0o755)
    script = tmp_path / "egress.sh"
    script.write_text(b.render_egress_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env.pop("POLYROB_BROWSER_CDP_CLIENTS", None)
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_text(), r.stderr


def test_cdp_rule_is_installed_when_no_client_uid_resolves(tmp_path):
    """Codex 067 follow-up #3: with no client identity on the host the port
    rule was omitted (fail open: every local UID could drive the browser). It
    is now always installed; the allowed set shrinks to root + the browser."""
    rules, err = _run_egress(tmp_path, {"polyrob-browser": 998})
    assert "tcp dport 9222 meta skuid != { 0, 998 } counter reject" in rules
    assert "ip6 daddr ::1 tcp dport 9222 meta skuid != { 0, 998 } counter reject" in rules
    assert "open to root and polyrob-browser only" in err


def test_cdp_rule_admits_the_clients_that_resolve(tmp_path):
    rules, _ = _run_egress(tmp_path, {"polyrob-browser": 998, "polyrob-agent": 997})
    assert "tcp dport 9222 meta skuid != { 0, 998, 997 } counter reject" in rules


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
