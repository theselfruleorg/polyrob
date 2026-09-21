"""`polyrob keys` — the owner's API-key seat (043 A30).

The HTTP endpoints POST/GET/DELETE ``/api/auth/api-keys`` (``api/auth_endpoints.py``)
mint, list and revoke the API keys that authenticate the A2A and OpenAI-compat
surfaces over ``X-API-KEY: rob_…`` — but every one of them requires
``request.state.user_id``, a wallet-SIWE JWT the headless prod console never
mints. So an owner running a headless box had no way to create a key at all.
This is that seat.

It is the owner's OWN machine, so it needs no JWT: it resolves the owner tenant
with :func:`core.instance.resolve_owner_user_id` (the ONE owner-tenant resolver
every attribution site reads) and drives
:class:`modules.auth.api_key_manager.APIKeyManager` directly against the SAME
``bot.db`` the running API validates keys from — so a key minted here works
immediately.

The secret is printed ONCE at creation and never stored in plain text: only its
sha256 hash and a 12-char prefix live in the table. ``list`` shows prefixes and
labels, never a secret; ``revoke`` deactivates by prefix.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import click

from cli._admin_home import as_root_option

# The ``api_keys`` DDL is owned by ``modules/database/auth_tables.py`` (the
# server's schema init); this is a byte-compatible ``IF NOT EXISTS`` mirror so
# the owner seat works on a box that has NEVER booted the server. When the
# server has run, this is a no-op. If the DDL there changes, change it there and
# mirror it here.
_API_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL,
    scopes TEXT DEFAULT '["*"]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    expires_at TIMESTAMP,
    is_active BOOLEAN DEFAULT 1,
    revoked_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
)
"""


class _CliDB:
    """Minimal async db seam over sqlite3 for :class:`APIKeyManager`.

    The manager needs only ``execute``/``fetch_one``/``fetch_all`` with the same
    shapes ``modules.database.database_manager.DatabaseManager`` exposes:
    ``execute(query, params_tuple)`` returns a cursor (``.rowcount`` for
    revoke); the fetches return dict rows. Foreign keys are left OFF (the sqlite
    default), so the owner tenant needs no ``user_profiles`` row to mint a key.
    The connection is created and used on the one thread ``asyncio.run`` drives,
    so ``check_same_thread`` is satisfied.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path))
        self._con.row_factory = sqlite3.Row
        self._con.execute(_API_KEYS_DDL)
        self._con.commit()

    async def execute(self, query: str, *args):
        cur = self._con.execute(query, *args)
        self._con.commit()
        return cur

    async def fetch_one(self, query: str, *args):
        row = self._con.execute(query, *args).fetchone()
        return dict(row) if row is not None else None

    async def fetch_all(self, query: str, *args):
        return [dict(r) for r in self._con.execute(query, *args).fetchall()]

    def close(self) -> None:
        self._con.close()


class _OwnerTier:
    """The DEN-token tier gate on :meth:`APIKeyManager.generate_api_key` is a
    platform-billing BETA control for multi-tenant SIWE users. On the owner's
    OWN machine the owner IS the owner, so this stub always grants a tier — the
    same reason this seat needs no wallet-SIWE JWT. It touches no money gate, no
    forged/leaf/tainted refusal, and no pause.
    """

    async def get_user_tier(self, user_id: str) -> str:
        return "owner"


def _bot_db_path(*, write: "bool | None" = None) -> Path:
    """The ``bot.db`` the API validates keys from.

    C7: this was a THIRD home resolver — ``POLYROB_DATA_DIR`` else
    ``resolve_data_home()`` else the literal string ``"data"``. On a deployed
    box with no ``POLYROB_DATA_DIR`` in the owner's shell it therefore MINTED a
    key into ``./data/database/bot.db``, a file the running API has never
    opened, and printed a secret the owner then could not authenticate with.
    Worse, the fallback CREATES that file, so the mistake left a decoy store
    behind. The seam is ``admin_data_dir`` (031), and an EXISTING bot.db under
    the manifest layout wins over the canonical path so a legacy install is not
    forked into a second file.
    """
    from cli._admin_home import admin_data_dir
    home = admin_data_dir(write=write)
    try:
        from core.db_manifest import candidate_sqlite_dbs
        for candidate in candidate_sqlite_dbs(home):
            if candidate.name == "bot.db" and candidate.is_file():
                return Path(candidate)
    except Exception:
        pass
    return Path(home) / "database" / "bot.db"


def _owner_user_id() -> str:
    """The owner tenant a key is minted for — the ONE admin resolver, so a key
    created in an SSH shell belongs to the tenant the service authenticates."""
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _manager_and_db(*, write: "bool | None" = None):
    from modules.auth.api_key_manager import APIKeyManager

    db = _CliDB(_bot_db_path(write=write))
    return APIKeyManager(db=db, tier_manager=_OwnerTier()), db


@click.group("keys")
def keys():
    """Create, list and revoke API keys for the A2A / OpenAI-compat surfaces.

    The keys authenticate over ``X-API-KEY: rob_…`` against the SAME store the
    API server reads — the ADMIN data home (``cli/_admin_home.py``, the 031
    rule), so a key minted in an SSH shell on a deployed box lands where the
    running service validates it (C7) and is usable immediately.
    """
    from cli.commands._bootstrap import ensure_env_loaded

    ensure_env_loaded()


@keys.command("list")
def list_cmd():
    """List this owner's API keys — prefixes and labels only, never a secret."""
    mgr, db = _manager_and_db(write=False)
    try:
        rows = asyncio.run(mgr.list_user_keys(_owner_user_id()))
    finally:
        db.close()
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("API keys",
                         "create one: `polyrob keys create --name <label>`"))
        return
    for r in rows:
        state = "active" if r.get("is_active") else "revoked"
        name = r.get("name") or "(unnamed)"
        prefix = r.get("key_prefix") or "?"
        expiry = r.get("expires_at") or "no expiry"
        click.echo(f"  {prefix}…  {name}  [{state}, {expiry}]")


@keys.command("create")
@click.option("--name", "-n", default="Default",
              help="A label for the key (shown by `list`).")
@click.option("--expires-days", type=int, default=None,
              help="Days until the key expires (default: never).")
@as_root_option
def create_cmd(name, expires_days):
    """Mint a new API key and print it ONCE.

    The secret is never stored in plain text and never reprinted — copy it now.
    """
    mgr, db = _manager_and_db(write=True)
    try:
        result = asyncio.run(mgr.generate_api_key(
            user_id=_owner_user_id(), name=name, expires_days=expires_days))
    except ValueError as exc:
        raise click.ClickException(str(exc))
    finally:
        db.close()
    click.echo(click.style(
        "API key created — copy it now, it is shown only once:", fg="green"))
    click.echo(f"\n  {result['api_key']}\n")
    click.echo(f"  label:   {result['name']}")
    click.echo(f"  prefix:  {result['prefix']}")
    click.echo(f"  expires: {result.get('expires_at') or 'never'}")
    click.echo('\nUse it with:  curl -H "X-API-KEY: <key>" <host>/a2a/rpc')


@keys.command("revoke")
@click.argument("prefix")
@as_root_option
def revoke_cmd(prefix):
    """Revoke the key with PREFIX (the 12-char `rob_…` shown by `list`)."""
    mgr, db = _manager_and_db(write=True)
    try:
        ok = asyncio.run(mgr.revoke_key(_owner_user_id(), prefix))
    finally:
        db.close()
    if not ok:
        raise click.ClickException(f"no active API key with prefix {prefix}")
    click.echo(f"revoked {prefix}")
