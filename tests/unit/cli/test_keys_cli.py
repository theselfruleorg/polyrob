"""`polyrob keys` — the owner's API-key seat (043 A30).

There was no CLI to mint an API key: the HTTP endpoints
(``api/auth_endpoints.py``) all require a wallet-SIWE JWT the headless console
never mints, so an owner running a headless box could not create a key for the
A2A / OpenAI-compat surfaces at all. This group is that seat — it resolves the
owner tenant with ``resolve_owner_user_id`` (not a JWT) and drives
``modules.auth.api_key_manager.APIKeyManager`` against the SAME ``bot.db`` the
API validates keys from.

These tests run with NO provider keys configured (a known CLI-test landmine):
the seat never builds the full container and never touches an LLM, so it must
work keyless.
"""
import re
import sqlite3
from pathlib import Path

from click.testing import CliRunner


def _extract_secret(output: str) -> str:
    """The full ``rob_…`` secret from a `create` run.

    The manager logs the 12-char PREFIX (``rob_abc…``) and the command also
    prints the prefix on its own line, so a bare ``re.search`` can grab a
    12-char token; the real secret is the LONGEST ``rob_`` match.
    """
    cands = re.findall(r"rob_[A-Za-z0-9_\-]+", output)
    assert cands, f"no rob_ token in output:\n{output}"
    return max(cands, key=len)


def _isolated(tmp_path, monkeypatch):
    """Isolate the env-file ladder AND the data home so the test never reads a
    real ``~/.polyrob/.env`` or writes a real ``bot.db``, and the owner tenant
    resolves to the default ``local`` (no bound owner)."""
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    data = tmp_path / "data"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data))
    # Force the default owner tenant (`local`): no bound owner principal.
    for var in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER"):
        monkeypatch.delenv(var, raising=False)
    # Reset the once-per-process env-load memo so the load runs under the
    # isolated home above, not whatever a prior test loaded.
    monkeypatch.setattr("cli.commands._bootstrap._env_loaded", False, raising=False)
    return data


def _bot_db(data: Path) -> Path:
    return data / "database" / "bot.db"


def _invoke(args, data_unused=None):
    from cli.commands.keys import keys
    return CliRunner().invoke(keys, args)


def test_create_mints_a_key_and_prints_it_once(tmp_path, monkeypatch):
    data = _isolated(tmp_path, monkeypatch)
    res = _invoke(["create", "--name", "my-agent"])
    assert res.exit_code == 0, res.output
    # A real `rob_` key is printed exactly once, with its label.
    assert "rob_" in res.output
    assert res.output.count("rob_") >= 1
    assert "my-agent" in res.output
    assert "shown only once" in res.output
    # It landed in the SAME bot.db the API reads, scoped to the owner tenant,
    # and only the HASH + prefix are stored — never the secret.
    con = sqlite3.connect(str(_bot_db(data)))
    try:
        rows = con.execute(
            "SELECT user_id, key_hash, key_prefix, name FROM api_keys").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    user_id, key_hash, key_prefix, name = rows[0]
    assert user_id == "local"          # owner tenant, resolved not from a JWT
    assert name == "my-agent"
    assert key_prefix.startswith("rob_")
    # The plaintext secret is nowhere in the row (only its sha256 hash).
    full = _extract_secret(res.output)
    assert len(full) > len(key_prefix)
    assert full != key_hash
    assert full not in key_hash


def test_list_shows_prefixes_never_the_secret(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    create = _invoke(["create", "--name", "alpha"])
    assert create.exit_code == 0, create.output
    full = _extract_secret(create.output)

    res = _invoke(["list"])
    assert res.exit_code == 0, res.output
    assert "alpha" in res.output
    assert full[:12] in res.output          # the 12-char prefix is shown
    assert full not in res.output           # the full secret is NEVER shown
    assert "active" in res.output


def test_revoke_by_prefix_removes_it(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    create = _invoke(["create", "--name", "throwaway"])
    assert create.exit_code == 0, create.output
    prefix = _extract_secret(create.output)[:12]

    rev = _invoke(["revoke", prefix])
    assert rev.exit_code == 0, rev.output
    assert prefix in rev.output
    assert "revoked" in rev.output.lower()

    # After revoke the key still lists but as revoked, not active.
    listed = _invoke(["list"])
    assert listed.exit_code == 0, listed.output
    assert "revoked" in listed.output.lower()


def test_revoke_unknown_prefix_is_a_clear_error(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    res = _invoke(["revoke", "rob_notreal000"])
    assert res.exit_code != 0
    assert "no active API key" in res.output


def test_list_empty_is_honest(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    res = _invoke(["list"])
    assert res.exit_code == 0, res.output
    assert "no API keys" in res.output


def test_list_is_tenant_scoped(tmp_path, monkeypatch):
    """A key belonging to another tenant is invisible to the owner's list."""
    data = _isolated(tmp_path, monkeypatch)
    # Seed a foreign-tenant key directly into the store.
    db_path = _bot_db(data)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
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
                revoked_at TIMESTAMP
            )
        """)
        con.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            ("someone_else", "deadbeefhash", "rob_stranger", "not-mine"))
        con.commit()
    finally:
        con.close()

    res = _invoke(["list"])
    assert res.exit_code == 0, res.output
    assert "not-mine" not in res.output
    assert "rob_stranger" not in res.output
    assert "no API keys" in res.output
