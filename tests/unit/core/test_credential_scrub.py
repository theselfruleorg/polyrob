"""Phase 0.5 — core.secret_scrub conservative secret-shape redaction.

This scrubber runs over the PERSISTED agent message history (not just CLI display),
so it must redact high-confidence credential shapes WITHOUT the aggressive
hex/base64 catch-alls the display scrubber uses — those would corrupt legitimate
working content (a hash the agent is comparing, a base64 blob it is processing).
"""
from core.secret_scrub import scrub_secret_shapes


def test_none_returns_empty():
    assert scrub_secret_shapes(None) == ""


def test_redacts_pem_private_key_block():
    text = (
        "here is the key\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\ndone"
    )
    out = scrub_secret_shapes(text)
    assert "PRIVATE KEY" not in out
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "here is the key" in out and "done" in out


def test_redacts_provider_sk_key():
    out = scrub_secret_shapes("OPENAI_API_KEY=sk-abc123DEF456ghi789JKL")
    assert "sk-abc123DEF456ghi789JKL" not in out


def test_redacts_aws_access_key_id():
    out = scrub_secret_shapes("aws id AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "here" in out


def test_redacts_bearer_token():
    out = scrub_secret_shapes("Authorization: Bearer eyJhbGciOi.JIUzI1NiJ9.abc")
    assert "eyJhbGciOi.JIUzI1NiJ9.abc" not in out


def test_redacts_key_value_credential_keeps_key_name():
    out = scrub_secret_shapes('password: "hunter2supersecret"')
    assert "hunter2supersecret" not in out
    assert "password" in out  # key name preserved for readability


def test_does_not_redact_plain_hex_hash():
    """Conservative: a 40-char hex (git SHA / file hash) is NOT a credential —
    redacting it would corrupt working content. This is the key difference from
    the display-only CLI scrubber."""
    sha = "a" * 40
    out = scrub_secret_shapes(f"commit {sha} fixed it")
    assert sha in out


def test_does_not_redact_long_base64_blob():
    blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"  # 48 b64 chars
    out = scrub_secret_shapes(f"data:{blob}")
    assert blob in out


def test_passthrough_normal_prose():
    text = "The agent read the file and summarized the report in three points."
    assert scrub_secret_shapes(text) == text


def test_redacts_prefixed_env_var_key():
    """P1 (finalization): a `<PREFIX>_API_KEY=` shape is the single most common
    real env-var naming, and the old `\\b`-anchored regex missed it (the `_` before
    API is a word char, so there is no boundary). It leaked verbatim from the
    scrubber used on PERSISTED message history / compaction checkpoints. The fix was
    already present in the cli/ui/secrets.py twin; this asserts core parity."""
    secret = "hunter2supersecretvalue"
    out = scrub_secret_shapes(f"MY_CUSTOM_API_KEY={secret}")
    assert secret not in out
    assert "MY_CUSTOM_API_KEY" in out  # key name kept, value redacted


def test_redacts_provider_prefixed_token():
    secret = "abcdef1234567890xyz"
    out = scrub_secret_shapes(f"GEMINI_AUTH_TOKEN={secret}")
    assert secret not in out
