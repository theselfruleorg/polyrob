"""032 — the app registry: one row per ``(slug, user_id)``.

Shape copied from ``tools/hf_deploy/registry.py::DeployedAppsRegistry`` (017 §9
said the same): SQLite via ``core.sqlite_util`` (WAL + jittered retry), tenant
scoping IN THE PRIMARY KEY, never in a filter an author can forget.

The row IS the owner ask: a NEW slug lands ``pending`` and nothing runs until an
owner seat calls :meth:`AppServiceRegistry.mark_approved`. Approval sticks to
the approved ADDRESS **and its approved CONFIGURATION**
(:mod:`core.app_service.fingerprint`): a redeploy of the same configuration goes
straight to ``approved`` (unattended within caps), while a request that moves the
command, the port, the health path, the egress policy or the env key set drops
back to ``pending`` naming what changed — and is REFUSED outright while the app is
``live``, because a running public address is stopped before it is reconfigured,
never rewritten underneath. The workspace digest is NOT part of the fingerprint,
so a code bump on the same configuration still redeploys unattended. A slug that
holds an address (approved/deploying/live/paused) is unique across tenants by a
partial unique index, so a collision REJECTS and never co-hosts.
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from core.app_service.env_scan import SECRET_KEY_RE, secret_value_reason
from core.app_service.fingerprint import (
    approval_config, changed_fields, config_from_row, describe_change, fingerprint,
)
from core.sqlite_util import execute_retry, wal_connect

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DEPLOYING = "deploying"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_PAUSED = "paused"
ALL_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DEPLOYING, STATUS_LIVE,
                STATUS_FAILED, STATUS_STOPPED, STATUS_PAUSED)
#: Statuses in which a row HOLDS its slug (the cross-tenant uniqueness set).
HOLDS_ADDRESS = (STATUS_APPROVED, STATUS_DEPLOYING, STATUS_LIVE, STATUS_PAUSED)
EGRESS_MODES = ("none", "allowlist", "open")

#: Mirror of ``tools/code_exec/env_policy.SECRET_PAT``, kept in
#: :mod:`core.app_service.env_scan` (core may not import tools; parity pinned by
#: tests/unit/core/app_service/test_app_registry.py).
_SECRET_KEY_RE = SECRET_KEY_RE
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MAX_ENV_ENTRIES = 64
_MAX_ENV_BYTES = 4096

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_services (
    slug                 TEXT NOT NULL,
    user_id              TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    source_dir           TEXT NOT NULL,
    cmd                  TEXT NOT NULL,
    container_port       INTEGER NOT NULL,
    health_path          TEXT NOT NULL DEFAULT '/',
    egress               TEXT NOT NULL DEFAULT 'none',
    egress_allow         TEXT,
    env_json             TEXT,
    workspace_digest     TEXT,
    host_port            INTEGER,
    container_name       TEXT,
    public_url           TEXT,
    approved_at          REAL,
    last_deploy          REAL,
    last_health          REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_error   TEXT,
    approved_fingerprint TEXT,
    approved_config      TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (slug, user_id)
);
CREATE INDEX IF NOT EXISTS idx_app_services_user   ON app_services(user_id);
CREATE INDEX IF NOT EXISTS idx_app_services_status ON app_services(status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_app_services_slug_holder
    ON app_services(slug) WHERE status IN ('approved','deploying','live','paused');

CREATE TABLE IF NOT EXISTS app_deploy_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    slug     TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_deploy_attempts_user ON app_deploy_attempts(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_app_deploy_attempts_app  ON app_deploy_attempts(slug, user_id, ts);
"""


def screen_env(env: Any) -> Dict[str, str]:
    """The declared app env, or ``ValueError``. Keys are ``[A-Z_][A-Z0-9_]*``,
    values strings; a secret-shaped key OR a credential-shaped VALUE is refused
    here — the ONE place the env enters the registry — so no host credential can
    be smuggled into a publicly reachable container via a "declared" variable,
    under an innocent name (``GREETING=sk-…``) or otherwise. Only the offending
    KEY is named; a screened value is never echoed."""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env must be a mapping of NAME -> value")
    if len(env) > _MAX_ENV_ENTRIES:
        raise ValueError(f"env has more than {_MAX_ENV_ENTRIES} entries")
    out: Dict[str, str] = {}
    total = 0
    for k, v in env.items():
        key = str(k)
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"env key {key!r} must match [A-Z_][A-Z0-9_]*")
        if _SECRET_KEY_RE.search(key):
            raise ValueError(f"env key {key!r} is secret-shaped; an app env never carries a credential")
        val = "" if v is None else str(v)
        shape = secret_value_reason(val)
        if shape:
            raise ValueError(
                f"env value for {key!r} looks like {shape}; an app env never carries a "
                f"credential (the container is publicly reachable)")
        total += len(key) + len(val)
        if total > _MAX_ENV_BYTES:
            raise ValueError(f"env exceeds {_MAX_ENV_BYTES} bytes")
        out[key] = val
    return out


def default_app_services_db(data_dir: Optional[str] = None) -> str:
    from core.runtime_paths import data_home_db_path
    return data_home_db_path("app_services.db", env_key="APP_SERVICES_DB_PATH",
                             data_dir=data_dir)


class AppServiceRegistry:
    """Tenant-keyed app rows with CAS transitions. Every method is safe to call
    from any process (the agent, the supervisor, an owner seat)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
            have = {row[1] for row in conn.execute("PRAGMA table_info(app_services)")}
            for col in ("approved_fingerprint", "approved_config"):
                if col not in have:  # a pre-fingerprint database, added in place
                    conn.execute(f"ALTER TABLE app_services ADD COLUMN {col} TEXT")
            conn.commit()
        finally:
            conn.close()

    # --- row shape ---------------------------------------------------------

    @staticmethod
    def _row(r) -> Optional[Dict[str, Any]]:
        """Parse the JSON columns and derive the approval view.

        Migration-safe: a row approved BEFORE the fingerprint existed carries no
        ``approved_fingerprint``, so it is derived from the configuration the row
        currently stores (i.e. exactly what the owner has been running). An old
        row therefore keeps redeploying unattended and never crashes a read.
        """
        if r is None:
            return None
        d = dict(r)
        d["cmd"] = json.loads(d.get("cmd") or "[]")
        d["egress_allow"] = json.loads(d.get("egress_allow") or "[]")
        d["env"] = json.loads(d.pop("env_json", None) or "{}")
        try:
            approved_cfg = json.loads(d.get("approved_config") or "null")
        except (TypeError, ValueError):
            approved_cfg = None
        current = config_from_row(d)
        if d.get("approved_at") and not d.get("approved_fingerprint"):
            approved_cfg = approved_cfg or current
            d["approved_fingerprint"] = fingerprint(approved_cfg)
        d["approved_config"] = approved_cfg
        d["approval_change"] = changed_fields(approved_cfg, current)
        return d

    # --- reads -------------------------------------------------------------

    def get(self, slug: str, user_id: str) -> Optional[Dict[str, Any]]:
        return self._row(execute_retry(
            self.db_path, "SELECT * FROM app_services WHERE slug=? AND user_id=?",
            (slug, user_id), fetch="one"))

    def list_for(self, user_id: str) -> List[Dict[str, Any]]:
        rows = execute_retry(
            self.db_path, "SELECT * FROM app_services WHERE user_id=? ORDER BY created_at, slug",
            (user_id,), fetch="all")
        return [self._row(r) for r in rows or []]

    def list_all(self) -> List[Dict[str, Any]]:
        rows = execute_retry(self.db_path, "SELECT * FROM app_services ORDER BY created_at, slug",
                             (), fetch="all")
        return [self._row(r) for r in rows or []]

    def list_by_status(self, statuses: Sequence[str]) -> List[Dict[str, Any]]:
        statuses = tuple(statuses)
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        rows = execute_retry(
            self.db_path,
            f"SELECT * FROM app_services WHERE status IN ({marks}) ORDER BY created_at, slug",
            statuses, fetch="all")
        return [self._row(r) for r in rows or []]

    def slug_holder(self, slug: str) -> Optional[str]:
        marks = ",".join("?" for _ in HOLDS_ADDRESS)
        r = execute_retry(
            self.db_path, f"SELECT user_id FROM app_services WHERE slug=? AND status IN ({marks})",
            (slug, *HOLDS_ADDRESS), fetch="one")
        return r["user_id"] if r else None

    def holding_count(self, user_id: str) -> int:
        marks = ",".join("?" for _ in HOLDS_ADDRESS)
        r = execute_retry(
            self.db_path,
            f"SELECT COUNT(*) AS n FROM app_services WHERE user_id=? AND status IN ({marks})",
            (user_id, *HOLDS_ADDRESS), fetch="one")
        return int(r["n"]) if r else 0

    def used_host_ports(self) -> set:
        rows = execute_retry(
            self.db_path, "SELECT host_port FROM app_services WHERE host_port IS NOT NULL",
            (), fetch="all")
        return {int(r["host_port"]) for r in rows or []}

    def deploys_in_last_day(self, user_id: str) -> int:
        r = execute_retry(
            self.db_path, "SELECT COUNT(*) AS n FROM app_deploy_attempts WHERE user_id=? AND ts>?",
            (user_id, time.time() - 86400), fetch="one")
        return int(r["n"]) if r else 0

    def last_attempt_epoch(self, slug: str, user_id: str) -> Optional[float]:
        r = execute_retry(
            self.db_path,
            "SELECT MAX(ts) AS ts FROM app_deploy_attempts WHERE slug=? AND user_id=?",
            (slug, user_id), fetch="one")
        return float(r["ts"]) if r and r["ts"] is not None else None

    # --- writes ------------------------------------------------------------

    def upsert_request(self, slug: str, user_id: str, *, source_dir: str, cmd: List[str],
                       container_port: int, health_path: str, egress: str,
                       egress_allow: List[str], env: Dict[str, str],
                       workspace_digest: str) -> Dict[str, Any]:
        """The agent's deploy request.

        ``pending`` for a never-approved slug OR for a request whose approval
        fingerprint differs from the one the owner approved (the returned row's
        ``approval_change`` names the fields that moved); ``approved`` (a queued
        redeploy, unattended) when the address is approved AND the requested
        configuration fingerprints identically. Raises ``PermissionError`` on a
        cross-tenant slug and ``ValueError`` on a malformed request, a deploy in
        progress, or a changed configuration for an app that is ``live`` (stop it
        first — a running public address is never rewritten underneath)."""
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
            raise ValueError("cmd must be a non-empty list of strings (an argv, never a shell line)")
        if not isinstance(container_port, int) or not (1 <= container_port <= 65535):
            raise ValueError("container_port must be an integer in 1..65535")
        if not isinstance(health_path, str) or not health_path.startswith("/"):
            raise ValueError("health_path must start with '/'")
        if egress not in EGRESS_MODES:
            raise ValueError(f"egress must be one of {', '.join(EGRESS_MODES)}")
        allow = [str(h) for h in (egress_allow or [])]
        clean_env = screen_env(env)
        holder = self.slug_holder(slug)
        if holder is not None and holder != user_id:
            raise PermissionError(f"slug {slug!r} is held by another tenant")
        now = time.time()
        existing = self.get(slug, user_id)
        if existing is None:
            execute_retry(
                self.db_path,
                "INSERT INTO app_services (slug, user_id, status, source_dir, cmd, container_port, "
                "health_path, egress, egress_allow, env_json, workspace_digest, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, user_id, STATUS_PENDING, source_dir, json.dumps(cmd), int(container_port),
                 health_path, egress, json.dumps(allow), json.dumps(clean_env), workspace_digest,
                 now, now))
            return self.get(slug, user_id)
        if existing["status"] == STATUS_DEPLOYING:
            raise ValueError(f"{slug!r} is deploying right now; request again after it finishes")
        requested = approval_config(cmd=cmd, container_port=container_port,
                                    health_path=health_path, egress=egress,
                                    egress_allow=allow, env=clean_env)
        same_config = (existing.get("approved_fingerprint") == fingerprint(requested))
        approved = bool(existing.get("approved_at")) and same_config
        if not approved and existing["status"] == STATUS_LIVE:
            # A LIVE row describes a container that is serving the address on the
            # configuration the owner approved. Writing the new configuration onto
            # it would leave the row and the container disagreeing (the supervisor
            # would reconcile the RUNNING app against rules nobody approved), and
            # dropping the row to `pending` would release its address. Refuse, and
            # say what to do — never silently reconfigure a live public address.
            changed = changed_fields(existing.get("approved_config"), requested)
            raise ValueError(
                f"{slug!r} is live on the configuration the owner approved and "
                f"{describe_change(changed)}; stop it first (app_stop {slug}) and request "
                f"the new configuration — a running public address is never reconfigured "
                f"without a new owner approval")
        status = STATUS_APPROVED if approved else STATUS_PENDING
        # An approved redeploy PERSISTS the (possibly derived) fingerprint, so an
        # old row is migrated on its first write. A request that differs leaves
        # the approved fingerprint/config alone: it still records what the owner
        # said yes to, so reverting to it redeploys unattended again.
        extra_sql, extra_args = "", ()
        if approved:
            extra_sql = ", approved_fingerprint=?, approved_config=?"
            extra_args = (fingerprint(requested), json.dumps(requested, sort_keys=True))
        execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, source_dir=?, cmd=?, container_port=?, "
            "health_path=?, egress=?, egress_allow=?, env_json=?, workspace_digest=?, "
            f"consecutive_failures=0, last_failure_error=NULL{extra_sql}, updated_at=? "
            "WHERE slug=? AND user_id=?",
            (status, source_dir, json.dumps(cmd), int(container_port), health_path, egress,
             json.dumps(allow), json.dumps(clean_env), workspace_digest, *extra_args,
             now, slug, user_id))
        return self.get(slug, user_id)

    def mark_approved(self, slug: str, user_id: str) -> bool:
        """Owner decision: ``pending`` -> ``approved`` (CAS; False when not pending).

        The approval is stamped with the fingerprint of the configuration the
        owner is looking at, so a later request that moves the command, port,
        health path, egress policy or env key set returns to ``pending``."""
        row = self.get(slug, user_id)
        if row is None or row["status"] != STATUS_PENDING:
            return False
        config = config_from_row(row)
        now = time.time()
        # CAS on updated_at as well as status: a request that lands between the
        # read and this write would otherwise stamp the owner's approval onto a
        # configuration they never saw. A concurrent write makes this return
        # False and the seat says "state changed underneath" — ask again.
        rc = execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, approved_at=?, approved_fingerprint=?, "
            "approved_config=?, updated_at=? "
            "WHERE slug=? AND user_id=? AND status=? AND updated_at=?",
            (STATUS_APPROVED, now, fingerprint(config), json.dumps(config, sort_keys=True),
             now, slug, user_id, STATUS_PENDING, row["updated_at"]))
        return rc == 1

    def approval_change_reason(self, row: Dict[str, Any]) -> Optional[str]:
        """Owner-readable: why this row is pending again, or ``None``."""
        fields = list(row.get("approval_change") or [])
        if not fields:
            return None
        return describe_change(fields)

    def claim(self, slug: str, user_id: str, from_status: str, to_status: str) -> bool:
        rc = execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, updated_at=? WHERE slug=? AND user_id=? AND status=?",
            (to_status, time.time(), slug, user_id, from_status))
        return rc == 1

    def set_status(self, slug: str, user_id: str, status: str, *,
                   error: Optional[str] = None) -> bool:
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        rc = execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, last_failure_error=COALESCE(?, last_failure_error), "
            "updated_at=? WHERE slug=? AND user_id=?",
            (status, error, time.time(), slug, user_id))
        return rc == 1

    def record_live(self, slug: str, user_id: str, *, host_port: int, container_name: str,
                    public_url: str) -> None:
        now = time.time()
        execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, host_port=?, container_name=?, public_url=?, "
            "last_deploy=?, last_health=?, consecutive_failures=0, last_failure_error=NULL, "
            "updated_at=? WHERE slug=? AND user_id=?",
            (STATUS_LIVE, int(host_port), container_name, public_url, now, now, now, slug, user_id))

    def record_failed(self, slug: str, user_id: str, *, error: str) -> None:
        execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, consecutive_failures=consecutive_failures+1, "
            "last_failure_error=?, updated_at=? WHERE slug=? AND user_id=?",
            (STATUS_FAILED, (error or "")[:500], time.time(), slug, user_id))

    def record_health(self, slug: str, user_id: str, ok: bool) -> None:
        now = time.time()
        if ok:
            execute_retry(
                self.db_path,
                "UPDATE app_services SET last_health=?, consecutive_failures=0, updated_at=? "
                "WHERE slug=? AND user_id=?", (now, now, slug, user_id))
        else:
            execute_retry(
                self.db_path,
                "UPDATE app_services SET consecutive_failures=consecutive_failures+1, updated_at=? "
                "WHERE slug=? AND user_id=?", (now, slug, user_id))

    def record_stopped(self, slug: str, user_id: str) -> None:
        """After the supervisor removed the container/stanza: the row keeps its
        approval (the address sticks) but no longer names a port or container."""
        execute_retry(
            self.db_path,
            "UPDATE app_services SET status=?, host_port=NULL, container_name=NULL, "
            "public_url=NULL, updated_at=? WHERE slug=? AND user_id=?",
            (STATUS_STOPPED, time.time(), slug, user_id))

    def record_attempt(self, slug: str, user_id: str) -> None:
        execute_retry(
            self.db_path, "INSERT INTO app_deploy_attempts (slug, user_id, ts) VALUES (?,?,?)",
            (slug, user_id, time.time()))
