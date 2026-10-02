"""032 H07 — approval provenance that lives OUTSIDE the agent-writable registry.

``app_services.db`` is group-writable by every ``polyrob-data`` identity, so a
``status='approved'`` column alone is not an owner decision: anything that can
write the db could "approve" its own row. An owner seat therefore stamps the
approval with an HMAC over ``(slug, user_id, fingerprint of the approved
configuration)``, keyed by a secret that the registry db does not hold, and the
root supervisor verifies the MAC against the row's CURRENT configuration before
it deploys anything. An unsigned or mismatched row is sent back to ``pending``
(the owner ask) — never deployed.

Where the key lives:

* prod (``/etc/polyrob`` exists): ``/etc/polyrob/app_approval.key``, created by
  root (``deployment/hardening/install-service-identities.sh`` or the supervisor
  on first tick) mode 0640, group ``polyrob-seat`` — the identities that host an
  owner seat (Telegram in ``polyrob-agent``, the console in ``polyrob-web``).
  ``polyrob-email`` and anything else in ``polyrob-data`` cannot read it.
* dev / a single-identity install: ``<dir of app_services.db>/app_approval.key``
  (0600, created on first use). There is no privilege boundary to defend there.

The workspace digest is deliberately NOT signed (the fingerprint excludes it, so a
code bump on the approved configuration still redeploys unattended); the
supervisor re-validates it and the source dir itself.
"""
import errno
import hashlib
import hmac
import os
import secrets
from typing import Optional

ETC_DIR = "/etc/polyrob"
ETC_KEY_PATH = os.path.join(ETC_DIR, "app_approval.key")
KEY_FILENAME = "app_approval.key"
SEAT_GROUP = "polyrob-seat"


class ApprovalKeyUnavailable(RuntimeError):
    """The approval key cannot be read (or created) by this process."""


def default_approval_key_path(db_path: str) -> str:
    """The prod key when the host carries one (or root can create it), else the
    dev key beside the registry db."""
    if os.path.exists(ETC_KEY_PATH):
        return ETC_KEY_PATH
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0 and os.path.isdir(ETC_DIR):
        return ETC_KEY_PATH
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), KEY_FILENAME)


def _seat_gid() -> Optional[int]:
    try:
        import grp
        return grp.getgrnam(SEAT_GROUP).gr_gid
    except (ImportError, KeyError):
        return None


def _create_key(path: str) -> None:
    prod = os.path.abspath(path) == ETC_KEY_PATH
    mode = 0o640 if prod else 0o600
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        if prod:
            gid = _seat_gid()
            if gid is not None:
                os.fchown(fd, 0, gid)
        os.write(fd, secrets.token_hex(32).encode("ascii"))
    finally:
        os.close(fd)


def load_key(path: str, *, create: bool = True) -> bytes:
    """The approval key bytes, or ``ApprovalKeyUnavailable``. Never follows a
    symlink; creates the key (O_EXCL) when *create* and it is missing."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if not create:
                raise ApprovalKeyUnavailable(f"approval key {path} does not exist")
            try:
                _create_key(path)
            except FileExistsError:
                pass  # a concurrent creator won; read theirs
            except OSError as e:
                raise ApprovalKeyUnavailable(f"cannot create approval key {path}: {e}")
            continue
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ApprovalKeyUnavailable(f"approval key {path} is a symlink; refused")
            raise ApprovalKeyUnavailable(f"cannot read approval key {path}: {e}")
        try:
            data = os.read(fd, 4096).strip()
        finally:
            os.close(fd)
        if len(data) < 32:
            raise ApprovalKeyUnavailable(f"approval key {path} is too short")
        return data
    raise ApprovalKeyUnavailable(f"approval key {path} could not be created")


def approval_mac(key: bytes, slug: str, user_id: str, fingerprint: str) -> str:
    msg = "\x1f".join(("polyrob-app-approval-v1", str(slug), str(user_id), str(fingerprint)))
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_mac(key: bytes, slug: str, user_id: str, fingerprint: str, mac: Optional[str]) -> bool:
    if not mac or not isinstance(mac, str):
        return False
    return hmac.compare_digest(approval_mac(key, slug, user_id, fingerprint), mac)
