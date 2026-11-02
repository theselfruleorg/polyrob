"""``polyrob doctor``: is the durable-app and static-publish serving side ready?

A managed user-mode install met each of these by hand (2026-10-29): a
certificate the agent uid cannot reach (certbot's 0700 ``live/``), a missing
nginx include dir, an app image that only a private factory builds, and a
``PUBLISH_ROOT`` under a 0750 home that nginx cannot read, with no
``PUBLISH_BASE_URL``. Silent when neither rail is in use. Read-only: it never
creates, chmods or pulls anything — ``polyrob apps install`` does that.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import List, Optional, Set, Tuple

_FIX = "polyrob apps install"
_PUBLISH_FIX_ROOT = "/var/www/polyrob-publish"


def _image_present(image: str) -> Optional[bool]:
    from cli.commands.apps_install import _docker_image_present
    return _docker_image_present(image)


def _apps_requested() -> bool:
    from core.app_service.config import app_service_enabled
    from core.config_policy.builder_mode import agent_builder_mode
    return agent_builder_mode() == "ship" or app_service_enabled()


def apps_lines() -> List[str]:
    from core.app_service import config as c
    from core.config_policy.builder_mode import cert_problem
    out: List[str] = []
    domain = c.app_service_base_domain()
    if not domain:
        return [f"apps: ! ship is requested but APP_SERVICE_BASE_DOMAIN is not set ({_FIX} --domain …)"]
    cert = os.path.join(c.app_service_cert_dir(), "fullchain.pem")
    problem = cert_problem(cert)
    out.append(f"apps cert: ok ({cert})" if problem is None else f"apps cert: ! {problem}")
    conf_dir = c.app_service_nginx_conf_dir()
    if os.path.isdir(conf_dir):
        out.append(f"apps nginx: ok ({conf_dir})")
    else:
        out.append(f"apps nginx: ! include dir {conf_dir} is missing — no app can go live ({_FIX})")
    image = c.app_service_image()
    present = _image_present(image)
    if present is True:
        out.append(f"apps image: ok ({image})")
    elif present is False:
        out.append(f"apps image: ! {image} is not pulled on this box — docker pull {image} ({_FIX} pulls it)")
    else:
        out.append(f"apps image: {image} not checked (docker not reachable as this user)")
    return out


def _nginx_identity() -> Tuple[str, Optional[int], Set[int]]:
    """``(name, uid, gids)`` of the nginx worker user (``user`` in nginx.conf, else
    www-data). An unknown user is judged by the "other" permission bits."""
    name = "www-data"
    try:
        for raw in Path("/etc/nginx/nginx.conf").read_text(encoding="utf-8").splitlines():
            parts = raw.split("#", 1)[0].strip().rstrip(";").split()
            if len(parts) >= 2 and parts[0] == "user":
                name = parts[1]
                break
    except OSError:
        pass
    try:
        import grp
        import pwd
        pw = pwd.getpwnam(name)
        gids = {pw.pw_gid} | {g.gr_gid for g in grp.getgrall() if name in g.gr_mem}
        return name, pw.pw_uid, gids
    except (KeyError, ImportError):
        return name, None, set()


def _ancestors(path: Path) -> List[Path]:
    path = Path(path)
    return list(reversed(path.parents)) + [path]


def _allowed(st: os.stat_result, uid: Optional[int], gids: Set[int], bits: int) -> bool:
    if uid == 0:
        return True
    if uid is not None and st.st_uid == uid:
        shift = 6
    elif st.st_gid in gids:
        shift = 3
    else:
        shift = 0
    return (st.st_mode >> shift) & bits == bits


def servable_problem(root: Path, who: Tuple[str, Optional[int], Set[int]]) -> Optional[str]:
    """Why the nginx user *who* cannot serve *root*, or None."""
    name, uid, gids = who
    chain = _ancestors(root)
    for d in chain:
        try:
            st = os.stat(d)
        except FileNotFoundError:
            return f"{d} does not exist"
        except OSError as exc:
            return f"{d} cannot be checked ({exc.strerror or exc})"
        last = d == chain[-1]
        need = (stat.S_IROTH | stat.S_IXOTH) if last else stat.S_IXOTH
        if not _allowed(st, uid, gids, need):
            verb = "read" if last else "traverse"
            return f"{name} cannot {verb} {d} ({stat.S_IMODE(st.st_mode):04o})"
    return None


def publish_lines() -> List[str]:
    from core.publish import default_base_url, default_publish_root
    root = Path(default_publish_root())
    who = _nginx_identity()
    out: List[str] = []
    problem = servable_problem(root, who)
    if problem is None:
        out.append(f"publish root: ok ({root}, readable by {who[0]})")
    else:
        out.append(f"publish root: ! {root} is not servable: {problem} — set "
                   f"PUBLISH_ROOT={_PUBLISH_FIX_ROOT} and run `{_FIX}` (it creates the root "
                   "and the static host)")
    base = default_base_url()
    if base:
        out.append(f"publish url: {base}")
    else:
        out.append("publish url: ! PUBLISH_BASE_URL is unset — publish returns a relative "
                   "/<slug>/ link nobody can open; set it to https://static.<APP_SERVICE_BASE_DOMAIN>")
    return out


def apps_publish_lines(env: dict) -> List[str]:
    from core.config_policy.builder_mode import publish_enabled
    out: List[str] = []
    if _apps_requested():
        out.extend(apps_lines())
    if publish_enabled():
        out.extend(publish_lines())
    return out
