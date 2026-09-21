"""C1 (2026-09-21) — the REPL display scrubber must not eat PUBLIC facts.

``cli/ui/secrets.py`` ends with two LENGTH-based catch-alls (hex >= 32, base64
>= 40). Every other rule in the battery matches a credential SHAPE; these two
match anything long, and ``/`` was in the base64 class — so a wallet address, a
transaction hash, a session UUID and every workspace path the REPL printed came
back as ``«redacted»``. The launch verb's own success line is ``token: 0x…``,
and the owner had to read his agent's freshly deployed token address off a
third-party indexer.

These tests pin BOTH directions: the public facts survive, and the credential
shapes still redact.

(Named ``test_scrubber_*`` rather than ``test_secrets_*``: the repo's
``.gitignore`` carries a ``*_SECRET*`` rule and, on a case-insensitive checkout,
a file named ``test_secrets_…`` is silently untracked.)
"""
import base64 as _base64
import hashlib as _hashlib

from cli.ui.secrets import REDACTED, scrub_secrets

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



# A real (public) Base address, a real-shaped tx hash, a Solana pubkey.
_EVM_ADDR = "0xA4f3AC4CF228Ad50fd36a08f65eD36E1973A616A"
_TX_HASH = "0x" + "ab" * 32
_SOL_ADDR = "2RLNRfAkjzP59AEx1yQD8Pw4YBKRydAPP4tJvSNvgS5Z"
_UUID = "4f8a1b2c-3d4e-5f60-8a9b-0c1d2e3f4a5b"


def test_wallet_line_survives():
    """The one line a money verb produces is the one thing it must keep."""
    line = f"evm / operational: {_EVM_ADDR} (receive)"
    assert scrub_secrets(line) == line
    assert scrub_secrets(f"token: {_EVM_ADDR}") == f"token: {_EVM_ADDR}"


def test_solana_address_survives():
    line = f"solana / account0: {_SOL_ADDR} (receive)"
    assert scrub_secrets(line) == line


def test_transaction_hash_survives():
    """A receipt hash IS the proof a money verb produced."""
    line = f"broadcast ok — tx {_TX_HASH}"
    assert scrub_secrets(line) == line


def test_uuid_and_undashed_uuid_survive():
    assert scrub_secrets(f"session {_UUID}") == f"session {_UUID}"
    flat = _UUID.replace("-", "")
    assert scrub_secrets(f"session {flat}") == f"session {flat}"


def test_absolute_workspace_path_survives():
    line = "wrote /Users/me/.polyrob/data/auto/local/sessions/abc/workspace/out.txt"
    assert scrub_secrets(line) == line


def test_relative_workspace_path_survives():
    """``/`` is out of the base64 class, so a relative path is segments now."""
    line = "feed at data/auto/local/sessions/abcdefabcdef/feed"
    assert scrub_secrets(line) == line


def test_dotted_identifier_survives():
    line = "raised in agents.task.agent.core.step_execution._build_execution_context"
    assert scrub_secrets(line) == line


# ---------------------------------------------------------------------------
# …and the credentials still go.
# ---------------------------------------------------------------------------


def test_provider_key_still_redacts():
    key = "sk-" + "proj-" + _hex("scrubber-openai", 32)
    out = scrub_secrets(f"OPENAI_API_KEY={key}")
    assert REDACTED in out
    assert key not in out


def test_named_credential_under_a_key_still_redacts():
    """An address-shaped value is exempt under the bare key ``token`` ONLY —
    ``access_token`` and friends never hold one."""
    out = scrub_secrets(f"access_token: {_SOL_ADDR}")
    assert REDACTED in out
    assert _SOL_ADDR not in out


def test_bare_base64_blob_still_redacts():
    blob = "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTBPSUw"
    out = scrub_secrets(f"auth blob {blob}")
    assert REDACTED in out
    assert blob not in out


def test_pem_block_still_redacts():
    pem = ("-----BEGIN PRIVATE KEY-----\nAAAABBBBCCCC\n-----END PRIVATE KEY-----")
    out = scrub_secrets(pem)
    assert REDACTED in out
    assert "AAAABBBBCCCC" not in out


def test_jwt_still_redacts():
    head = _base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = _base64.urlsafe_b64encode(b'{"sub":"1234567890"}').decode().rstrip("=")
    jwt = f"{head}.{body}.{_b64url('scrubber-jwt-sig')}"
    out = scrub_secrets(f"Authorization value {jwt}")
    assert REDACTED in out
    assert head not in out


# ---------------------------------------------------------------------------
# Revalidation (2026-09-21): the C1 exemptions widened the public-fact hole, so
# the credential shapes on the other side of it are pinned EXPLICITLY here.
# Each string below is a real credential SHAPE, and each must still redact.
# ---------------------------------------------------------------------------

#: A raw secp256k1 private key as `polyrob wallet export` prints it — 64 hex,
#: NO `0x`. The hex catch-all is the only rule that claims it, so the C1
#: exemptions (`_TX_HASH_RE` needs the `0x`, `_UUID_RE` is 32 hex exactly,
#: `_BASE58_RE` caps at 44) must not reach it.
_PRIVKEY_NO_PREFIX = _hex("scrubber-privkey", 64)

#: A Fernet key (`MCP_ENCRYPTION_KEY`) — 44 urlsafe-base64 chars with `=` pad.
_FERNET = "cUJ1bGRfa2V5X2Zvcl90ZXN0aW5nX29ubHlfMTIzNDU2Nzg5MA=="

_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")


def test_bare_private_key_still_redacts():
    """The wallet's seed material is the worst thing this scrubber can leak."""
    out = scrub_secrets(f"imported {_PRIVKEY_NO_PREFIX} into the keystore")
    assert _PRIVKEY_NO_PREFIX not in out and REDACTED in out


def test_fernet_key_under_its_env_name_still_redacts():
    out = scrub_secrets(f"MCP_ENCRYPTION_KEY={_FERNET}")
    assert "cUJ1bGRfa2V5" not in out and REDACTED in out


def test_bearer_token_still_redacts():
    token = _hex("bearer-token", 40)
    out = scrub_secrets(f"Authorization: Bearer {token}")
    assert token not in out and REDACTED in out


def test_aws_access_key_id_still_redacts():
    out = scrub_secrets("AKIAIOSFODNN7EXAMPLE is the id")
    assert "AKIAIOSFODNN7EXAMPLE" not in out and REDACTED in out


def test_jwt_redacts_as_one_unit():
    """The opaque catch-alls run AFTER the named battery, so a JWT must come
    out as ONE marker rather than three redacted segments."""
    out = scrub_secrets(f"refresh={_JWT}")
    assert _JWT.split(".")[1] not in out
    assert out.count(REDACTED) == 1
