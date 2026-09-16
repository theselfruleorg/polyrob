"""M1 (wallet-security evaluation, 2026-09-14): the credential files POLYROB
writes for ITSELF are refused by the file-editing tools.

``is_credential_file`` is the guard ``tools/filesystem`` and ``tools/coding``
consult. It listed the world's credential shapes (``.env*``, ``*.pem``,
``id_rsa``) and none of POLYROB's own: ``.mcp_encryption_key`` ends in ``_key``
not ``.key``, and the three JSON stores have ordinary names. Under
``POLYROB_LOCAL`` the workspace IS the project cwd, so the Fernet key that
decrypts the MCP credential store, the X login and the OAuth token store was an
ordinary readable/writable file to the agent.

The names here are asserted against the CONSTANTS their owning modules write,
not against literals, so a rename cannot silently un-protect a file.
"""
from pathlib import Path

import pytest

from core.security.secret_guard import is_credential_file, is_secret_path

ROOT = Path("/ws")


def _both(name: str, *, under: str = "") -> tuple:
    p = ROOT / under / name if under else ROOT / name
    return is_credential_file(p), is_secret_path(p, root=ROOT)


# ---------------------------------------------------------------------------
# The names, read from their owning modules
# ---------------------------------------------------------------------------

def test_the_protected_names_are_the_ones_the_code_actually_writes():
    from core.instance import AGENT_MAIL_STATE_FILENAME
    from core.security.encryption import _key_file_path
    from tools.oauth.file_store import TOKENS_FILENAME
    from tools.x_browser.session_store import SESSION_FILENAME

    assert AGENT_MAIL_STATE_FILENAME == "agent_mail.json"
    assert _key_file_path().name == ".mcp_encryption_key"
    assert TOKENS_FILENAME == ".mcp_oauth_tokens.json"
    assert SESSION_FILENAME == ".x_session.json"


@pytest.mark.parametrize("name", [
    "bot.db",                  # sessions, users, API keys, billing rows
    ".mcp_encryption_key",     # the Fernet master key
    ".mcp_oauth_tokens.json",  # live OAuth access/refresh tokens
    ".x_session.json",         # the X login (cookies + localStorage)
    "agent_mail.json",         # the provisioned inbox id + address
])
def test_polyrob_credential_files_are_refused(name):
    cred, secret = _both(name)
    assert cred, f"{name} is editable by the filesystem/coding tools"
    assert secret, f"{name} can be ingested into model context"


@pytest.mark.parametrize("name", [
    ".x_session.9f2a.tmp",          # the store's atomic-write temp file
    ".mcp_oauth_tokens.abcd.tmp",
    ".mcp_encryption_key.bak",
])
def test_the_sibling_and_temp_files_are_refused_too(name):
    """The atomic writers name their temp file by PREFIX, so an exact-name glob
    would leave a full-value copy of the credential unprotected for the length
    of a write."""
    cred, _ = _both(name)
    assert cred, f"{name} (a live copy of a credential) is not refused"


def test_refused_wherever_they_live_not_only_under_data():
    """Before M1 these were caught only by the ``data/`` DIRECTORY rule in
    ``is_secret_path`` — which ``is_credential_file`` does not apply at all, and
    which a copy outside a ``data/`` dir escapes entirely."""
    for under in ("", "data", "backup", "workspace/out"):
        cred, _ = _both(".mcp_encryption_key", under=under)
        assert cred, f"not refused under {under!r}"


# ---------------------------------------------------------------------------
# The narrowness is deliberate — do not widen this list into is_secret_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "secrets.py",         # a Python module, refused by is_secret_path's *secret*
    "credentials.py",     # ditto, via *credential*
    "data.db",
    "main.py",
    "agent_mail_threads.json",  # a thread-id map, not a credential
])
def test_ordinary_project_files_stay_editable(name):
    """``is_credential_file`` is applied to arbitrary project files in local
    mode. Unioning it with ``is_secret_path`` (or importing the
    ``*secret*``/``*credential*`` substrings) would refuse to edit a Python
    ``secrets.py`` — a different failure, not a smaller one."""
    assert not is_credential_file(ROOT / name), name


def test_an_env_template_is_still_editable():
    assert not is_credential_file(ROOT / ".env.example")


def test_public_identity_record_is_credential_equivalent():
    """S1 (2026-09-14): the seedless units render every 'pay to' address from
    `wallet/public_identity.json`, so an agent must not be able to rewrite it."""
    from pathlib import Path
    from core.security.secret_guard import is_credential_file
    assert is_credential_file(Path("/var/lib/polyrob/wallet/public_identity.json"))
    assert is_credential_file(Path("data/wallet/public_identity.json.tmp123"))
    assert is_credential_file(Path("/x/.polyrob/wallet/public_identity.json"))
    assert not is_credential_file(Path("/x/wallet/public_report.json"))
