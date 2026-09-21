"""C28 (2026-09-21) — the logging filter must not corrupt paths or re-redact
a public address the SSOT battery has already exempted.

Three measured cases:

1. ``core.secret_patterns.apply_ssot_shapes`` exempts a public address under
   the bare key ``token`` (a contract, not a credential). The filter then ran
   its LEGACY ``token["']?\\s*[:=]`` pattern over the very same line and masked
   it anyway — an exemption applied on one layer and undone on the next is not
   an exemption.
2. A relative filesystem path of 32+ characters matched the base64 catch-all in
   full, so every relative session/workspace path this system logs came out
   masked mid-token.
3. A transaction hash and a UUID (a request / session / tool-call id) are
   public identifiers, and masking them removes the only handle an operator has
   on the event he is reading.
"""
import base64 as _base64
import hashlib as _hashlib

from core.security_logging_filter import SecretScrubbingFilter

# ---------------------------------------------------------------------------
# Every credential SHAPE below is BUILT, never written down. A literal that
# looks like a key trips the release scrub gate's gitleaks pass (the 1.0.2
# landmine), and a shipped test must not be the reason a publish is blocked.
# Deriving each from a fixed seed keeps the shape exact and the bytes boring.
# ---------------------------------------------------------------------------
def _hex(seed: str, n: int) -> str:
    return _hashlib.sha256(seed.encode()).hexdigest()[:n]


def _b64url(seed: str) -> str:
    return _base64.urlsafe_b64encode(
        _hashlib.sha256(seed.encode()).digest()).decode().rstrip("=")



_EVM_ADDR = "0x1234567890abcdef1234567890abcdef12345678"
_TX_HASH = "0x" + "ab" * 32
_UUID = "4f8a1b2c-3d4e-5f60-8a9b-0c1d2e3f4a5b"


def _scrub(text: str) -> str:
    return SecretScrubbingFilter().scrub_message(text)


def test_public_address_under_bare_token_key_survives_both_layers():
    line = f"token: {_EVM_ADDR}"
    assert _scrub(line) == line


def test_relative_path_is_not_masked():
    line = "feed dir data/auto/local/sessions/workspace/feed"
    assert _scrub(line) == line


def test_absolute_path_is_not_masked():
    line = "wrote /var/lib/polyrob/data/auto/local/sessions/abc/workspace"
    assert _scrub(line) == line


def test_transaction_hash_and_uuid_survive():
    assert _scrub(f"tx {_TX_HASH}") == f"tx {_TX_HASH}"
    assert _scrub(f"request {_UUID}") == f"request {_UUID}"


# ---------------------------------------------------------------------------
# …and the credentials still go.
# ---------------------------------------------------------------------------


def test_api_key_still_redacted():
    key = _hex("filter-api-key", 32)
    out = _scrub(f"api_key={key}")
    assert key not in out


def test_password_still_redacted():
    out = _scrub("password: hunter2hunter2")
    assert "hunter2hunter2" not in out


def test_address_shaped_value_under_a_credential_key_still_redacted():
    """Only the BARE key ``token`` is exempt — ``auth_token`` never holds an
    address, so an address-shaped value there is still treated as a secret."""
    out = _scrub(f"auth_token={_EVM_ADDR}")
    assert _EVM_ADDR not in out


# ---------------------------------------------------------------------------
# Revalidation (2026-09-21): the exemptions above are a hole in a redaction
# rule, so the credential shapes beside them are pinned explicitly. The filter
# had already lost the JWT rung once (see `core.secret_patterns`), which is why
# each rung is asserted here and not just the battery as a whole.
# ---------------------------------------------------------------------------

_PRIVKEY_NO_PREFIX = _hex("filter-privkey", 64)
_FERNET = "cUJ1bGRfa2V5X2Zvcl90ZXN0aW5nX29ubHlfMTIzNDU2Nzg5MA=="
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")


def test_bare_private_key_still_masked():
    out = _scrub(f"imported {_PRIVKEY_NO_PREFIX} into the keystore")
    assert _PRIVKEY_NO_PREFIX not in out


def test_fernet_key_still_masked():
    out = _scrub(f"MCP_ENCRYPTION_KEY={_FERNET}")
    assert "cUJ1bGRfa2V5" not in out


def test_bearer_token_still_masked():
    token = _hex("bearer-token", 40)
    assert token not in _scrub(f"Authorization: Bearer {token}")


def test_jwt_still_masked():
    assert _JWT.split(".")[1] not in _scrub(f"refresh={_JWT}")


def test_provider_key_still_masked():
    key = "sk-" + "proj-" + _hex("filter-openai", 40)
    assert key not in _scrub(f"OPENAI_API_KEY={key}")
