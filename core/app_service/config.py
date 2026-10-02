"""032 — the app-service flag readers (docs/CONFIGURATION.md rows are the SSOT)."""
import os
import re
from typing import Any, Mapping, Optional, Tuple

from core.config_policy.builder_mode import _builder_capability_default, base_domain, cert_dir_for
from core.env import bool_env, float_env, int_env

RESERVED_SLUGS = frozenset({"www", "api", "apps", "console", "pub", "mail", "admin", "status",
                            "ns", "ns1", "ns2", "mx", "smtp", "imap", "autoconfig"})
EGRESS_MODES = ("none", "allowlist", "open")


def app_service_enabled() -> bool:
    """Register the ``app_service`` tool + arm the supervisor. Default OFF; ON under
    effective ``AGENT_BUILDER_MODE=ship``; NOT flipped by POLYROB_LOCAL."""
    return bool_env("APP_SERVICE_ENABLED", _builder_capability_default("APP_SERVICE_ENABLED"))


def app_service_allow_public() -> bool:
    return bool_env("APP_SERVICE_ALLOW_PUBLIC",
                    _builder_capability_default("APP_SERVICE_ALLOW_PUBLIC"))


def app_service_base_domain() -> str:
    return base_domain()


def app_service_max_live() -> int:
    return max(0, int_env("APP_SERVICE_MAX_LIVE", 3))


def app_service_daily_max() -> int:
    return max(0, int_env("APP_SERVICE_DAILY_MAX", 10))


def app_service_min_interval_sec() -> int:
    return max(0, int_env("APP_SERVICE_MIN_INTERVAL_SEC", 120))


def app_service_memory_mb() -> int:
    return max(64, int_env("APP_SERVICE_MEMORY_MB", 512))


def app_service_cpus() -> float:
    return max(0.1, float_env("APP_SERVICE_CPUS", 0.5))


def app_service_pids() -> int:
    return max(16, int_env("APP_SERVICE_PIDS", 256))


def app_service_snapshot_max_mb() -> int:
    return max(1, int_env("APP_SERVICE_SNAPSHOT_MAX_MB", 500))


def app_service_egress_default() -> str:
    raw = (os.getenv("APP_SERVICE_EGRESS_DEFAULT") or "none").strip().lower()
    return raw if raw in EGRESS_MODES else "none"


def app_service_port_range() -> Tuple[int, int]:
    raw = (os.getenv("APP_SERVICE_PORT_RANGE") or "18000-18099").strip()
    try:
        lo_s, hi_s = raw.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        if 1024 <= lo <= hi <= 65535:
            return lo, hi
    except ValueError:
        pass
    return 18000, 18099


def app_service_tick_sec() -> int:
    return max(5, int_env("APP_SERVICE_TICK_SEC", 30))


def app_service_health_timeout_sec() -> int:
    return max(5, int_env("APP_SERVICE_HEALTH_TIMEOUT_SEC", 90))


def app_service_image() -> str:
    return ((os.getenv("APP_SERVICE_IMAGE") or os.getenv("CODE_EXEC_DEV_IMAGE") or "").strip()
            or "polyrob-dev:latest")


def app_service_retain_days() -> int:
    return max(0, int_env("APP_SERVICE_RETAIN_DAYS", 7))


def app_service_cert_dir() -> str:
    return cert_dir_for(base_domain())


def app_service_nginx_conf_dir() -> str:
    return (os.getenv("APP_SERVICE_NGINX_CONF_DIR") or "/etc/nginx/apps.d").strip().rstrip("/")


def apps_root(data_dir: Optional[str]) -> str:
    """Where snapshots and logs live: ``<data_dir>/apps``."""
    if not data_dir:
        from core.runtime_config import get_data_root
        data_dir = get_data_root()
    return os.path.join(data_dir, "apps")


def safe_tenant(user_id: str) -> str:
    """A tenant id as a filesystem/container-name segment (``[a-z0-9-]``)."""
    out = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(user_id or "").lower())
    return out.strip("-") or "tenant"


def app_dir(data_dir: Optional[str], user_id: str, slug: str) -> str:
    return os.path.join(apps_root(data_dir), safe_tenant(user_id), slug)


def logs_path(data_dir: Optional[str], user_id: str, slug: str) -> str:
    return os.path.join(app_dir(data_dir, user_id, slug), "logs.txt")


# --- H07: the row fields the ROOT supervisor turns into paths/names ----------
#
# The registry db is group-writable by every ``polyrob-data`` identity, so the
# supervisor must never trust a row field it uses for a filesystem op, a
# container name or a label. These are checked on the way IN (the registry) and
# AGAIN by the supervisor before anything touches the disk.

_DIGEST_RE = re.compile(r"[0-9a-f]{12,64}")
_MAX_TENANT_LEN = 128


def valid_app_slug(slug: Any) -> bool:
    from core.publish import valid_slug
    return valid_slug(slug) and slug not in RESERVED_SLUGS


def valid_app_tenant(user_id: Any) -> bool:
    from core.instance import is_safe_tenant_id
    return (isinstance(user_id, str) and 0 < len(user_id) <= _MAX_TENANT_LEN
            and is_safe_tenant_id(user_id))


def valid_workspace_digest(digest: Any) -> bool:
    return isinstance(digest, str) and bool(_DIGEST_RE.fullmatch(digest))


def row_field_error(row: Mapping[str, Any]) -> Optional[str]:
    """Why a registry row is unsafe to act on, or ``None``. Never echoes the
    raw value beyond a short repr (it is attacker-controlled text)."""
    if not valid_app_slug(row.get("slug")):
        return f"invalid slug {str(row.get('slug'))[:40]!r}"
    if not valid_app_tenant(row.get("user_id")):
        return f"invalid tenant {str(row.get('user_id'))[:40]!r}"
    if not valid_workspace_digest(row.get("workspace_digest")):
        return "invalid workspace digest (want 12-64 lowercase hex)"
    return None
