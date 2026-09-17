"""polyrob browser — the isolated browser service a custody deployment connects to (049).

A wallet-custody process never launches Chromium beside the signer; it connects
to a browser running as its OWN principal. This group makes that principal a
boundary rather than a label, on the same host:

- a dedicated system user, systemd-hardened, no custody env, no data home;
- the Chromium SANDBOX ON — Ubuntu 24.04 restricts unprivileged user namespaces
  through AppArmor, so a 6-line profile (Ubuntu's own ``chrome`` template with
  the path changed) lifts it for this binary only; ``--no-sandbox`` is never
  written;
- a UID-keyed nft egress chain: the browser may not open connections to
  loopback services (the console, its own CDP port), RFC1918, link-local or the
  cloud metadata service. The Playwright route guard is the second line.
- the CDP listener on ``127.0.0.1`` only. CDP has no authentication by design;
  moving the browser to a second host means a Playwright server over
  ``BROWSER_WSS_URL`` with a token path, never CDP over a network.

Subcommands: ``status`` (the rail line + the three host facts), ``install``
(user, Chromium for the pinned Playwright, unit, profile, egress; prints the
one env line; ``--mode server --listen <private-ip>`` on a SECOND host writes a
Playwright-server unit instead and prints the ``BROWSER_WSS_URL`` — phase 5,
which removes the shared kernel), ``update`` (re-install the browser for the
current Playwright pin and restart), ``render`` (print a template — the files
under ``deployment/`` are this output, pinned by a test).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

SERVICE_USER = "polyrob-browser"
INSTALL_ROOT = Path("/opt/polyrob-browser")
DATA_ROOT = Path("/var/lib/polyrob-browser")
UNIT_PATH = Path("/etc/systemd/system/polyrob-browser.service")
EGRESS_UNIT_PATH = Path("/etc/systemd/system/polyrob-browser-egress.service")
EGRESS_SCRIPT_PATH = Path("/usr/local/sbin/polyrob-browser-egress.sh")
APPARMOR_PROFILE_PATH = Path("/etc/apparmor.d/polyrob-browser")
DEFAULT_PORT = 9222
DEFAULT_SERVER_PORT = 3000

UNIT_TEMPLATE = """[Unit]
Description=POLYROB isolated browser endpoint (049)
Documentation=https://polyrob.dev/docs/self-hosting
After=network-online.target polyrob-browser-egress.service
Wants=network-online.target
Requires=polyrob-browser-egress.service

[Service]
Type=simple
User={user}
Group={user}
Environment=HOME={data}
Environment=XDG_RUNTIME_DIR=/run/polyrob-browser
RuntimeDirectory=polyrob-browser
RuntimeDirectoryMode=0700
# The Chromium SANDBOX IS ON. Ubuntu 24.04 restricts unprivileged user
# namespaces through AppArmor (kernel.apparmor_restrict_unprivileged_userns=1);
# /etc/apparmor.d/polyrob-browser lifts that for this binary only. Never add
# the sandbox-disabling flag here: this process renders untrusted pages on the
# signer's kernel, and the sandbox is the boundary. No POLYROB custody
# credential is ever present in this unit's environment.
ExecStart={root}/chrome --headless=new --disable-gpu --remote-debugging-address=127.0.0.1 --remote-debugging-port={port} --user-data-dir={data}/profile --no-first-run --no-default-browser-check --disable-extensions --disable-background-networking --disable-component-update --disable-sync --no-pings --disable-features=Translate,OptimizationHints,MediaRouter about:blank
Restart=always
RestartSec=3
MemoryMax=1500M
TasksMax=512
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RemoveIPC=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""

SERVER_ENV_PATH = Path("/etc/polyrob/browser-server.env")

SERVER_UNIT_TEMPLATE = """[Unit]
Description=POLYROB isolated browser server (049 phase 5 — second host, Playwright protocol)
Documentation=https://polyrob.dev/docs/self-hosting
After=network-online.target polyrob-browser-egress.service
Wants=network-online.target
Requires=polyrob-browser-egress.service

[Service]
Type=simple
User={user}
Group={user}
Environment=HOME={data}
Environment=XDG_RUNTIME_DIR=/run/polyrob-browser
Environment=PLAYWRIGHT_BROWSERS_PATH={root}
RuntimeDirectory=polyrob-browser
RuntimeDirectoryMode=0700
# The token (the URL path) is the only authentication the Playwright protocol
# has. It lives in {env} (root, 0600), never in this unit. Bind to the PRIVATE
# interface only; the agent host reaches it over the private network or a
# WireGuard tunnel — never expose this port to the public internet.
EnvironmentFile={env}
# The Chromium SANDBOX IS ON for every browser this server launches (see the
# AppArmor profile /etc/apparmor.d/polyrob-browser). No POLYROB custody
# credential is ever present on this host.
ExecStart={python} -m playwright run-server --host ${{BROWSER_SERVER_HOST}} --port ${{BROWSER_SERVER_PORT}} --path /${{BROWSER_SERVER_TOKEN}}
Restart=always
RestartSec=3
MemoryMax=2G
TasksMax=1024
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RemoveIPC=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths={data}

[Install]
WantedBy=multi-user.target
"""

EGRESS_UNIT_TEMPLATE = """[Unit]
Description=POLYROB isolated browser egress rules (no loopback / RFC1918 / link-local / metadata from the browser UID)
Before=polyrob-browser.service
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={script}
ExecStop=/usr/sbin/nft delete table inet polyrob_browser

[Install]
WantedBy=multi-user.target
"""

EGRESS_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# POLYROB isolated browser egress (049). The browser UID renders untrusted
# pages; it may not OPEN a connection to loopback services (the console, its
# own CDP port), RFC1918, carrier-grade NAT, link-local or the cloud metadata
# service. Replies on connections the agent opened (CDP on 127.0.0.1) are
# established traffic and pass; DNS to the local resolver stub is allowed
# explicitly. The Playwright route guard in the agent is the second line;
# this chain is the first. Idempotent: the table is replaced on every run.
set -euo pipefail
USER_NAME="${{POLYROB_BROWSER_USER:-{user}}}"
id -u "$USER_NAME" >/dev/null
# Loopback resolvers (systemd-resolved's 127.0.0.53, a local unbound, …) must
# stay reachable on 53 or the browser cannot resolve anything.
DNS_RULES=""
while read -r _ ns _; do
  case "$ns" in 127.*) DNS_RULES+="    ip daddr $ns udp dport 53 accept
    ip daddr $ns tcp dport 53 accept
";; esac
done < <(grep -E '^nameserver ' /etc/resolv.conf 2>/dev/null || true)
nft -f - <<EOF
table inet polyrob_browser
delete table inet polyrob_browser
table inet polyrob_browser {{
  chain output {{
    type filter hook output priority filter; policy accept;
    meta skuid != "$USER_NAME" accept
    ct state established,related accept
${{DNS_RULES}}    ip daddr {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 }} counter drop
    ip6 daddr {{ ::1/128, fe80::/10, fc00::/7 }} counter drop
  }}
}}
EOF
"""

APPARMOR_PROFILE_TEMPLATE = """# POLYROB isolated browser (049): lift Ubuntu 24.04's unprivileged-userns
# restriction (kernel.apparmor_restrict_unprivileged_userns=1) for THIS binary
# only, so Chromium's zygote sandbox works and --no-sandbox is never needed
# beside a wallet signer. This is Ubuntu's own /etc/apparmor.d/chrome template
# with the path changed; the glob follows the versioned Playwright layout and
# covers both binaries Playwright launches (chrome, and headless_shell for
# headless runs from a Playwright server).
abi <abi/4.0>,
include <tunables/global>

profile polyrob-browser {root}/{{,**/}}{{chrome,headless_shell}} flags=(unconfined) {{
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/polyrob-browser>
}}
"""


def render_unit(*, user: str = SERVICE_USER, root: Path = INSTALL_ROOT,
                data: Path = DATA_ROOT, port: int = DEFAULT_PORT) -> str:
    return UNIT_TEMPLATE.format(user=user, root=root, data=data, port=port)


def render_server_unit(*, user: str = SERVICE_USER, root: Path = INSTALL_ROOT,
                       data: Path = DATA_ROOT, env: Path = SERVER_ENV_PATH,
                       python: str = "/opt/polyrob/venv/bin/python") -> str:
    return SERVER_UNIT_TEMPLATE.format(user=user, root=root, data=data, env=env, python=python)


def render_egress_unit(*, script: Path = EGRESS_SCRIPT_PATH) -> str:
    return EGRESS_UNIT_TEMPLATE.format(script=script)


def render_egress_script(*, user: str = SERVICE_USER) -> str:
    return EGRESS_SCRIPT_TEMPLATE.format(user=user)


def render_apparmor_profile(*, root: Path = INSTALL_ROOT) -> str:
    return APPARMOR_PROFILE_TEMPLATE.format(root=root)


TEMPLATES = {
    "unit": render_unit,
    "server-unit": render_server_unit,
    "egress-unit": render_egress_unit,
    "egress-script": render_egress_script,
    "apparmor": render_apparmor_profile,
}


# --- host facts (Linux) ------------------------------------------------------

def playwright_chromium_revision() -> str:
    """The chromium revision the INSTALLED playwright package expects (its
    browsers.json), or '' when playwright is absent."""
    try:
        import playwright  # noqa: F401
        base = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        data = json.loads(base.read_text(encoding="utf-8"))
        for b in data.get("browsers", []):
            if b.get("name") == "chromium":
                return str(b.get("revision") or "")
    except Exception:
        return ""
    return ""


def installed_chromium_revision(root: Path = INSTALL_ROOT) -> str:
    """The revision the service symlink points at (chromium-<rev>/...), or ''."""
    link = root / "chrome"
    try:
        target = os.readlink(link) if link.is_symlink() else str(link)
    except OSError:
        return ""
    for part in Path(target).parts:
        if part.startswith("chromium-"):
            return part[len("chromium-"):]
    return ""


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def unit_active(name: str = "polyrob-browser.service") -> str:
    if shutil.which("systemctl") is None:
        return "n/a"
    return (_run(["systemctl", "is-active", name]).stdout or "").strip() or "unknown"


def apparmor_profile_loaded() -> str:
    try:
        text = Path("/sys/kernel/security/apparmor/profiles").read_text(encoding="utf-8")
    except OSError:
        return "n/a"
    return "loaded" if any(line.startswith("polyrob-browser ") for line in text.splitlines()) else "MISSING"


def egress_table_present() -> str:
    if shutil.which("nft") is None:
        return "n/a"
    rc = _run(["nft", "list", "table", "inet", "polyrob_browser"]).returncode
    return "present" if rc == 0 else "MISSING"


def _unit_is_server() -> bool:
    try:
        return "playwright run-server" in UNIT_PATH.read_text(encoding="utf-8")
    except OSError:
        return False


def unit_has_no_sandbox() -> str:
    try:
        text = UNIT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "n/a"
    return "PRESENT (remove it)" if "--no-sandbox" in text else "absent"


# --- commands ---------------------------------------------------------------

@click.group(name="browser")
def browser():
    """The isolated browser service a custody deployment connects to."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@browser.command("status")
def status():
    """The browser rail as this process sees it, plus the host facts."""
    from core.security.browser_rail import browser_rail_status
    rail = browser_rail_status(refresh=True)
    click.echo(f"browser rail: {rail.line()}")
    if not sys.platform.startswith("linux"):
        return
    pin, have = playwright_chromium_revision(), installed_chromium_revision()
    mode = "server" if _unit_is_server() else "cdp"
    click.echo(f"unit: {unit_active()} ({mode}) · --no-sandbox: {unit_has_no_sandbox()}")
    click.echo(f"apparmor profile: {apparmor_profile_loaded()} · egress chain: {egress_table_present()}")
    click.echo(f"chromium: installed {have or 'none'} · playwright pin {pin or 'unknown'}"
               + ("" if not pin or not have or pin == have else " — DRIFT, run `polyrob browser update`"))


@browser.command("render")
@click.argument("which", type=click.Choice(sorted(TEMPLATES)))
def render(which: str):
    """Print one of the installed files (unit / egress-unit / egress-script / apparmor)."""
    click.echo(TEMPLATES[which](), nl=False)


def _require_root():
    if os.geteuid() != 0:
        raise click.ClickException(
            f"needs root — run: sudo {sys.executable} -m cli.polyrob browser install")


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _install_chromium(root: Path, user: str, with_deps: bool) -> str:
    """Install the pinned Playwright Chromium under *root* and point root/chrome at it."""
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in ("SEED", "KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    cmd = [sys.executable, "-m", "playwright", "install"] + (["--with-deps"] if with_deps else []) + ["chromium"]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise click.ClickException(f"playwright install failed:\n{res.stderr[-2000:]}")
    # Playwright's layout is chromium-<rev>/chrome-linux64/chrome (older: chrome-linux/).
    candidates = sorted(root.glob("chromium-*/chrome-linux*/chrome"))
    if not candidates:
        raise click.ClickException(f"no chromium binary under {root} after install")
    binary = candidates[-1]
    link = root / "chrome"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(binary.relative_to(root))
    _run(["chown", "-R", f"{user}:{user}", str(root)])
    return installed_chromium_revision(root)


@browser.command("install")
@click.option("--mode", type=click.Choice(["cdp", "server"]), default="cdp", show_default=True,
              help="cdp = same host as the agent (loopback CDP); server = a SECOND host running a "
                   "Playwright server the agent reaches over BROWSER_WSS_URL.")
@click.option("--port", default=None, type=int, help="Listener port (cdp: 9222, server: 3000).")
@click.option("--listen", default=None,
              help="server mode: the PRIVATE IP to bind (never 0.0.0.0 on a public interface).")
@click.option("--with-deps", is_flag=True, help="Also install Chromium's system libraries (apt).")
@click.option("--dry-run", is_flag=True, help="Print what would be written; change nothing.")
def install(mode: str, port: int | None, listen: str | None, with_deps: bool, dry_run: bool):
    """Create the service user, install Chromium, write the unit/profile/egress, start it."""
    port = port or (DEFAULT_PORT if mode == "cdp" else DEFAULT_SERVER_PORT)
    if mode == "server" and not listen:
        raise click.UsageError("--mode server needs --listen <private-ip>")
    if mode == "server" and listen in ("0.0.0.0", "::", "*"):
        raise click.UsageError("bind the server to a PRIVATE interface address, not every interface")
    if dry_run:
        names = ("server-unit" if mode == "server" else "unit", "egress-unit", "egress-script", "apparmor")
        for name in names:
            click.echo(f"--- {name}")
            click.echo(TEMPLATES[name](**({"port": port} if name == "unit" else {})), nl=False)
        return
    if not sys.platform.startswith("linux"):
        raise click.ClickException("the isolated browser service is a Linux systemd unit")
    _require_root()
    if mode == "server":
        _install_server(port=port, listen=listen, with_deps=with_deps)
        return
    if _run(["id", "-u", SERVICE_USER]).returncode != 0:
        _run(["useradd", "--system", "--home-dir", str(DATA_ROOT), "--create-home",
              "--shell", "/usr/sbin/nologin", SERVICE_USER])
        click.echo(f"created user {SERVICE_USER}")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "profile").mkdir(exist_ok=True)
    _run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(DATA_ROOT)])
    os.chmod(DATA_ROOT, 0o700)
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    rev = _install_chromium(INSTALL_ROOT, SERVICE_USER, with_deps)
    click.echo(f"chromium {rev} installed under {INSTALL_ROOT}")
    _write(EGRESS_SCRIPT_PATH, render_egress_script(), 0o755)
    _write(EGRESS_UNIT_PATH, render_egress_unit())
    _write(UNIT_PATH, render_unit(port=port))
    _write(APPARMOR_PROFILE_PATH, render_apparmor_profile())
    if shutil.which("apparmor_parser"):
        res = _run(["apparmor_parser", "-r", str(APPARMOR_PROFILE_PATH)])
        click.echo("apparmor profile loaded" if res.returncode == 0
                   else f"apparmor_parser failed: {res.stderr.strip()[:300]}")
    else:
        click.echo("apparmor_parser not found — the sandbox needs the profile on Ubuntu 24.04")
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "--now", "polyrob-browser-egress.service"])
    res = _run(["systemctl", "restart", "polyrob-browser.service"])
    _run(["systemctl", "enable", "polyrob-browser.service"])
    if res.returncode != 0:
        raise click.ClickException(f"polyrob-browser.service failed to start: {res.stderr.strip()[:300]}")
    os.environ["BROWSER_CDP_URL"] = f"http://127.0.0.1:{port}"
    from core.security.browser_rail import browser_rail_status
    import time
    for _ in range(10):  # Chromium needs a moment to open the listener
        rail = browser_rail_status(refresh=True)
        if rail.ok:
            break
        time.sleep(1.0)
    click.echo(f"browser rail: {rail.line()}")
    if not rail.ok:
        raise click.ClickException(
            "the service started but the endpoint does not answer — "
            "`journalctl -u polyrob-browser.service -n 50` (a zygote/sandbox "
            "failure means the AppArmor profile did not load)")
    click.echo(f"add to the agent's env file (e.g. /etc/polyrob/polyrob.env):\n"
               f"  BROWSER_CDP_URL=http://127.0.0.1:{port}")


def _install_server(*, port: int, listen: str, with_deps: bool) -> None:
    """Second-host mode: a Playwright server the agent reaches over BROWSER_WSS_URL."""
    import secrets
    from importlib.metadata import version as _v
    if _run(["id", "-u", SERVICE_USER]).returncode != 0:
        _run(["useradd", "--system", "--home-dir", str(DATA_ROOT), "--create-home",
              "--shell", "/usr/sbin/nologin", SERVICE_USER])
        click.echo(f"created user {SERVICE_USER}")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(DATA_ROOT)])
    os.chmod(DATA_ROOT, 0o700)
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    rev = _install_chromium(INSTALL_ROOT, SERVICE_USER, with_deps)
    click.echo(f"chromium {rev} installed under {INSTALL_ROOT}")
    # The token is generated once and kept only in the root-only env file; a
    # re-install keeps the existing token so the agent's URL stays valid.
    token = None
    if SERVER_ENV_PATH.exists():
        for line in SERVER_ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("BROWSER_SERVER_TOKEN="):
                token = line.split("=", 1)[1].strip() or None
    token = token or secrets.token_urlsafe(32)
    _write(SERVER_ENV_PATH,
           f"BROWSER_SERVER_HOST={listen}\nBROWSER_SERVER_PORT={port}\nBROWSER_SERVER_TOKEN={token}\n",
           0o600)
    _write(EGRESS_SCRIPT_PATH, render_egress_script(), 0o755)
    _write(EGRESS_UNIT_PATH, render_egress_unit())
    _write(UNIT_PATH, render_server_unit(python=sys.executable))
    _write(APPARMOR_PROFILE_PATH, render_apparmor_profile())
    if shutil.which("apparmor_parser"):
        res = _run(["apparmor_parser", "-r", str(APPARMOR_PROFILE_PATH)])
        click.echo("apparmor profile loaded" if res.returncode == 0
                   else f"apparmor_parser failed: {res.stderr.strip()[:300]}")
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "--now", "polyrob-browser-egress.service"])
    res = _run(["systemctl", "restart", "polyrob-browser.service"])
    _run(["systemctl", "enable", "polyrob-browser.service"])
    if res.returncode != 0:
        raise click.ClickException(f"polyrob-browser.service failed to start: {res.stderr.strip()[:300]}")
    import time
    ok = False
    for _ in range(10):
        if _probe_tcp_ok(listen, port):
            ok = True
            break
        time.sleep(1.0)
    if not ok:
        raise click.ClickException(
            "the server started but does not listen — `journalctl -u polyrob-browser.service -n 50`")
    pw_version = _v("playwright")
    click.echo(f"playwright server listening on ws://{listen}:{port}/<token> (playwright {pw_version})")
    click.echo("on the AGENT host, add to its env file and restart the agent:\n"
               f"  BROWSER_WSS_URL=ws://{listen}:{port}/{token}\n"
               f"the agent's venv must carry playwright=={pw_version} (the Playwright protocol "
               "requires the same version on both ends). Allow inbound TCP "
               f"{port} from the agent's private IP only.")


def _probe_tcp_ok(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


@browser.command("update")
@click.option("--with-deps", is_flag=True)
def update(with_deps: bool):
    """Re-install Chromium for the CURRENT Playwright pin and restart the service."""
    if not sys.platform.startswith("linux"):
        raise click.ClickException("the isolated browser service is a Linux systemd unit")
    _require_root()
    before = installed_chromium_revision()
    rev = _install_chromium(INSTALL_ROOT, SERVICE_USER, with_deps)
    res = _run(["systemctl", "restart", "polyrob-browser.service"])
    if res.returncode != 0:
        raise click.ClickException(f"restart failed: {res.stderr.strip()[:300]}")
    click.echo(f"chromium {before or 'none'} → {rev}; service restarted")
