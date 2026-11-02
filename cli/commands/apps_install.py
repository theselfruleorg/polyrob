"""``polyrob apps install`` — the serving side of durable apps + static publish,
rendered for the layout THIS box runs.

Two layouts exist:

* **system** — the server deploy: ``/opt/polyrob`` + ``/etc/polyrob/polyrob.env``,
  de-rooted units in group ``polyrob-data``. The rendered unit is the shipped
  ``deployment/polyrob-apps.service`` (a test keeps the two equal).
* **user** — a managed user-mode install: one unix user, ``~/.polyrob`` (venv,
  ``.env`` with the wallet seed), a ``systemctl --user`` ``polyrob.service``
  (``polyrob service install``). The supervisor still runs as ROOT (docker and
  nft are root-equivalent anyway); its unit pins that user's ``POLYROB_HOME`` and
  data home, reads its flags from a seed-free ``/etc/polyrob/apps.env``, and makes
  the agent's ``.env`` INACCESSIBLE — the wallet seed never reaches it.

What runs where follows ``polyrob service``: as the service's user nothing
root-only is touched — the files are rendered into ``<POLYROB_HOME>/apps-install/``
and the exact ``sudo`` commands are printed. As root the steps are performed.
``--dry-run`` prints the plan and the file contents and changes nothing.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import click

UNIT_PATH = "/etc/systemd/system/polyrob-apps.service"
APPS_ENV_PATH = "/etc/polyrob/apps.env"
INCLUDE_CONF_PATH = "/etc/nginx/conf.d/polyrob-apps-include.conf"
STATIC_CONF_PATH = "/etc/nginx/conf.d/polyrob-publish.conf"
DEFAULT_PUBLISH_ROOT = "/var/www/polyrob-publish"
SYSTEM_ENV_FILE = "/etc/polyrob/polyrob.env"
SYSTEM_CODE_DIR = "/opt/polyrob"

#: The keys a supervisor may need, copied into the seed-free apps.env. Nothing
#: else of the agent's env (keys, tokens, the seed) is ever copied.
_SUPERVISOR_KEYS_PREFIX = "APP_SERVICE_"
_SUPERVISOR_KEYS = ("CODE_EXEC_DEV_IMAGE",)


@dataclass
class Layout:
    kind: str                 # "system" | "user"
    agent_user: str
    agent_group: str
    polyrob_home: str         # user layout only ("" on system)
    data_dir: str
    agent_env_file: str       # the file that holds the agent's secrets
    workdir: str
    exec_start: str


@dataclass
class Step:
    what: str
    argv: List[str] = field(default_factory=list)   # a command to run
    path: str = ""                                  # or a file to write
    content: str = ""
    mode: int = 0o644


def _group_name(gid: int) -> str:
    import grp
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def detect_layout(*, is_root: bool, user: Optional[str] = None,
                  system_present: Optional[bool] = None,
                  service_users: Optional[Callable[[], list]] = None) -> Layout:
    """The layout of THIS box. Raises ``click.ClickException`` when it is ambiguous."""
    import pwd
    if system_present is None:
        system_present = (Path(SYSTEM_CODE_DIR, "venv").is_dir()
                          and Path(SYSTEM_ENV_FILE).is_file())
    if system_present and not user:
        data = os.environ.get("POLYROB_DATA_DIR") or "/var/lib/polyrob"
        return Layout("system", "polyrob-agent", "polyrob-data", "", data, SYSTEM_ENV_FILE,
                      SYSTEM_CODE_DIR, f"{SYSTEM_CODE_DIR}/venv/bin/polyrob apps supervise")

    if is_root:
        from cli.commands._service_ops import parse_unit
        from cli.commands._service_ops import service_users as _su
        users = (service_users or _su)()
        if user:
            users = [u for u in users if u[0] == user]
        if not users:
            raise click.ClickException(
                "no server layout (/opt/polyrob) and no polyrob.service user unit found — "
                "run `polyrob service install` as the agent's user first, or pass --user NAME")
        if len(users) > 1:
            raise click.ClickException("several users run a polyrob service: "
                                       + ", ".join(u[0] for u in users) + " — pick one with --user NAME")
        name, _uid, unit = users[0]
        argv, env = parse_unit(Path(unit).read_text(encoding="utf-8"))
        pw = pwd.getpwnam(name)
        home = env.get("POLYROB_HOME") or str(Path(pw.pw_dir) / ".polyrob")
        python = argv[0] if argv and Path(argv[0]).name.startswith("python") else sys.executable
        workdir, group = pw.pw_dir, _group_name(pw.pw_gid)
        data = env.get("POLYROB_DATA_DIR") or str(Path(home) / "data")
    else:
        from core.paths import polyrob_home
        from core.runtime_paths import resolve_data_home
        pw = pwd.getpwuid(os.geteuid())
        name, home, python = pw.pw_name, str(polyrob_home()), sys.executable
        workdir, group = pw.pw_dir, _group_name(pw.pw_gid)
        data = str(resolve_data_home())
    return Layout("user", name, group, home, data, str(Path(home) / ".env"), workdir,
                  f"{shlex.quote(python)} -I -m cli.polyrob apps supervise")


_UNIT_HEAD = """# POLYROB durable app service supervisor (proposal 032) — `polyrob apps supervise`.
#
# Rendered by `polyrob apps install` for the {kind} layout. The agent never holds
# the docker/nginx/nft privilege for this rail; this unit does, as root (docker
# group membership is root-equivalent, so a non-root user would not reduce it).
# It never needs the wallet seed: the agent's env file is inaccessible to it.

[Unit]
Description=POLYROB durable app service supervisor (polyrob apps supervise)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=500
StartLimitBurst=5

[Service]
Type=simple
User=root
Group={group}
UMask=0002
WorkingDirectory={workdir}
Environment="PYTHONUNBUFFERED=1"
{env_lines}ExecStart={exec_start}
Restart=on-failure
RestartSec=10
TimeoutStopSec=60
KillMode=mixed
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=polyrob-apps
MemoryMax=512M

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths={rw_paths}
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictNamespaces=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
InaccessiblePaths={inaccessible}

[Install]
WantedBy=multi-user.target
"""


def render_unit(layout: Layout, *, nginx_dir: str) -> str:
    if layout.kind == "system":
        env_lines = f"EnvironmentFile=-{SYSTEM_ENV_FILE}\n"
        rw = f"-{nginx_dir}"
        inaccessible = "-/etc/polyrob/wallet.env -/var/lib/polyrob-signer"
    else:
        env_lines = (f'Environment="POLYROB_HOME={layout.polyrob_home}"\n'
                     f'Environment="POLYROB_DATA_DIR={layout.data_dir}"\n'
                     f"EnvironmentFile=-{APPS_ENV_PATH}\n")
        rw = f"-{nginx_dir} {layout.data_dir}"
        inaccessible = (f"-{layout.agent_env_file} -/etc/polyrob/wallet.env "
                        "-/var/lib/polyrob-signer")
    return _UNIT_HEAD.format(kind=layout.kind, group=layout.agent_group, workdir=layout.workdir,
                             env_lines=env_lines, exec_start=layout.exec_start, rw_paths=rw,
                             inaccessible=inaccessible)


def render_apps_env(environ, *, base_domain: str) -> str:
    """The supervisor's seed-free env file: ship mode + the APP_SERVICE_* flags."""
    lines = ["# POLYROB apps supervisor env — written by `polyrob apps install`.",
             "# Only app-service flags live here; the agent's secrets never do.",
             "AGENT_BUILDER_MODE=ship", f"APP_SERVICE_BASE_DOMAIN={base_domain}"]
    for key in sorted(environ):
        if key == "APP_SERVICE_BASE_DOMAIN":
            continue
        if key.startswith(_SUPERVISOR_KEYS_PREFIX) or key in _SUPERVISOR_KEYS:
            value = str(environ[key])
            if "\n" not in value:
                lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def render_include(nginx_dir: str) -> str:
    return ("# POLYROB durable app service (032): one server block per live app, written by\n"
            f"# polyrob-apps.service into {nginx_dir} — never edited by hand or by the agent.\n"
            f"include {nginx_dir}/*.conf;\n")


@dataclass
class Checks:
    lines: List[str]
    cert_ok_for_root: bool
    image_missing: bool


def _docker_image_present(image: str, runner=subprocess.run) -> Optional[bool]:
    if not shutil.which("docker"):
        return None
    try:
        res = runner(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001 — a probe
        return None
    if res.returncode == 0:
        return True
    err = (res.stderr or "").lower()
    if "permission denied" in err or "cannot connect" in err:
        return None
    return False


def preflight(*, base_domain: str, cert_dir: str, image: str,
              which=shutil.which, image_present=None) -> Checks:
    from core.config_policy.builder_mode import cert_problem
    image_present = image_present or _docker_image_present
    lines: List[str] = []
    lines.append(f"docker: {'found' if which('docker') else 'MISSING — install docker first'}")
    lines.append(f"nginx:  {'found' if which('nginx') else 'MISSING — install nginx first'}")
    cert = os.path.join(cert_dir, "fullchain.pem")
    problem = cert_problem(cert)
    cert_ok_for_root = problem is None or "not readable by" in problem
    if problem is None:
        lines.append(f"cert:   ok ({cert})")
    elif "not readable by" in problem:
        lines.append(f"cert:   {problem}")
    else:
        lines.append(f"cert:   {problem}. A wildcard needs DNS-01; the renewable path is "
                     "certbot-dns-cloudflare (docs/guide/deployment-postures.md#durable-apps):")
        lines.append("        certbot certonly --dns-cloudflare --dns-cloudflare-credentials "
                     f"/root/.secrets/cloudflare.ini -d '*.{base_domain}' -d '{base_domain}'")
    present = image_present(image)
    image_missing = present is False
    if present is True:
        lines.append(f"image:  {image} is present")
    elif present is False:
        lines.append(f"image:  {image} is not pulled — the install pulls it")
    else:
        lines.append(f"image:  {image} not checked (docker not reachable as this user)")
    return Checks(lines, cert_ok_for_root, image_missing)


def build_plan(layout: Layout, *, base_domain: str, cert_dir: str, nginx_dir: str,
               publish_root: Optional[str], image: str, image_missing: bool,
               cert_ok: bool, environ) -> List[Step]:
    from core.app_service.nginx import render_static_vhost
    steps: List[Step] = [Step(f"supervisor unit ({layout.kind} layout)", path=UNIT_PATH,
                              content=render_unit(layout, nginx_dir=nginx_dir))]
    if layout.kind == "user":
        steps.append(Step("seed-free supervisor env", path=APPS_ENV_PATH,
                          content=render_apps_env(environ, base_domain=base_domain), mode=0o640))
    steps.append(Step("per-app nginx include dir", argv=["install", "-d", "-m", "0755", nginx_dir]))
    steps.append(Step("nginx include of that dir", path=INCLUDE_CONF_PATH,
                      content=render_include(nginx_dir)))
    if publish_root:
        steps.append(Step("static publish root (agent writes, nginx reads)",
                          argv=["install", "-d", "-o", layout.agent_user, "-g", layout.agent_group,
                                "-m", "0755", publish_root]))
        if cert_ok:
            steps.append(Step("static publish host", path=STATIC_CONF_PATH,
                              content=render_static_vhost(base_domain=base_domain, cert_dir=cert_dir,
                                                          root=publish_root)))
    if image_missing:
        steps.append(Step("app base image", argv=["docker", "pull", image]))
    steps.append(Step("validate nginx", argv=["nginx", "-t"]))
    steps.append(Step("reload nginx", argv=["systemctl", "reload", "nginx"]))
    steps.append(Step("systemd reload", argv=["systemctl", "daemon-reload"]))
    steps.append(Step("start the supervisor",
                      argv=["systemctl", "enable", "--now", "polyrob-apps.service"]))
    return steps


def agent_env_lines(*, base_domain: str, publish_root: Optional[str],
                    cert_dir_override: str = "") -> List[str]:
    lines = ["AGENT_BUILDER_MODE=ship", f"APP_SERVICE_BASE_DOMAIN={base_domain}"]
    if cert_dir_override:
        lines.append(f"APP_SERVICE_CERT_DIR={cert_dir_override}")
    if publish_root:
        from core.app_service.nginx import STATIC_LABEL
        lines += [f"PUBLISH_ROOT={publish_root}",
                  f"PUBLISH_BASE_URL=https://{STATIC_LABEL}.{base_domain}"]
    return lines


def _write(step: Step) -> None:
    os.makedirs(os.path.dirname(step.path), exist_ok=True)
    tmp = step.path + ".polyrob-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(step.content)
    os.chmod(tmp, step.mode)
    os.replace(tmp, step.path)


def execute(steps: Sequence[Step], *, echo=click.echo, runner=subprocess.run) -> int:
    """Perform the plan as root; stop at the first failure."""
    for step in steps:
        if step.path:
            _write(step)
            echo(f"  wrote {step.path}  ({step.what})")
            continue
        res = runner(step.argv, capture_output=True, text=True)
        if res.returncode != 0:
            echo(f"  FAILED {' '.join(step.argv)}: {(res.stderr or res.stdout or '').strip()[:400]}")
            return 1
        echo(f"  ran {' '.join(shlex.quote(a) for a in step.argv)}  ({step.what})")
    return 0


def sudo_script(steps: Sequence[Step], stage_dir: Path) -> List[str]:
    """Stage each file under *stage_dir* (as this user) and return the root commands."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for step in steps:
        if step.path:
            staged = stage_dir / os.path.basename(step.path)
            staged.write_text(step.content, encoding="utf-8")
            out.append(f"sudo install -D -m {step.mode:04o} {shlex.quote(str(staged))} "
                       f"{shlex.quote(step.path)}")
        else:
            out.append("sudo " + " ".join(shlex.quote(a) for a in step.argv))
    return out


@click.command("install")
@click.option("--domain", default=None, help="Base domain (default: APP_SERVICE_BASE_DOMAIN).")
@click.option("--publish-root", default=None,
              help=f"Directory nginx serves static publications from (default {DEFAULT_PUBLISH_ROOT}).")
@click.option("--no-publish", is_flag=True, help="Do not set up the static publish host.")
@click.option("--user", "as_user", default=None, metavar="NAME",
              help="As root on a user-mode box: the user that runs polyrob.service.")
@click.option("--dry-run", is_flag=True, help="Print the plan and the files; change nothing.")
def install_cmd(domain, publish_root, no_publish, as_user, dry_run):
    """Set up the serving side of durable apps + static publish for THIS box's layout."""
    from core.app_service import config as c
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    base = (domain or c.app_service_base_domain() or "").strip().lower().rstrip(".")
    if not base:
        raise click.ClickException("no base domain: pass --domain apps.example.com "
                                   "(or set APP_SERVICE_BASE_DOMAIN)")
    if domain:
        os.environ["APP_SERVICE_BASE_DOMAIN"] = base
    layout = detect_layout(is_root=is_root, user=as_user)
    cert_dir = c.app_service_cert_dir()
    nginx_dir = c.app_service_nginx_conf_dir()
    image = c.app_service_image()
    publish = None if no_publish else (publish_root or DEFAULT_PUBLISH_ROOT)

    click.echo(f"layout: {layout.kind} — agent user {layout.agent_user}:{layout.agent_group}, "
               f"data {layout.data_dir}")
    if layout.kind == "user":
        click.echo(f"        the supervisor cannot read {layout.agent_env_file} (the wallet seed)")
        click.echo("WARNING: in the user layout the root supervisor runs the agent user's own "
                   f"interpreter and code ({layout.exec_start.split()[0]}). Code the agent can "
                   "write (its venv, an editable tree) then runs as root at the next supervisor "
                   "restart. Use the system layout (root-owned /opt/polyrob) on a box where "
                   "that matters.")
    checks = preflight(base_domain=base, cert_dir=cert_dir, image=image)
    for ln in checks.lines:
        click.echo(ln)
    steps = build_plan(layout, base_domain=base, cert_dir=cert_dir, nginx_dir=nginx_dir,
                       publish_root=publish, image=image, image_missing=checks.image_missing,
                       cert_ok=checks.cert_ok_for_root, environ=os.environ)
    if publish and not checks.cert_ok_for_root:
        click.echo("static host: skipped until the certificate exists (re-run after certbot)")

    click.echo("\nplan:")
    for i, step in enumerate(steps, 1):
        target = step.path or " ".join(shlex.quote(a) for a in step.argv)
        click.echo(f"  {i}. {step.what}: {target}")
    env_lines = agent_env_lines(base_domain=base, publish_root=publish,
                                cert_dir_override=(os.getenv("APP_SERVICE_CERT_DIR") or "").strip())
    if dry_run:
        for step in steps:
            if step.path:
                click.echo(f"\n--- {step.path} (mode {step.mode:04o}) ---\n{step.content}", nl=False)
        rc = 0
    elif is_root:
        click.echo("\nperforming (root):")
        rc = execute(steps)
    else:
        stage = Path(layout.polyrob_home or str(Path.home() / ".polyrob")) / "apps-install"
        cmds = sudo_script(steps, stage)
        click.echo(f"\nnot root: the files are staged in {stage}. Run as root:")
        for cmd in cmds:
            click.echo(f"  {cmd}")
        rc = 0
    click.echo(f"\nadd to {layout.agent_env_file}, then restart the agent "
               f"({'polyrob service restart' if layout.kind == 'user' else 'systemctl restart polyrob.service'}):")
    for ln in env_lines:
        click.echo(f"  {ln}")
    if rc:
        raise SystemExit(rc)
