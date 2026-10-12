from pathlib import Path

import pytest

from core.secret_scrub import scrub_secret_shapes
from core.security.secret_guard import is_credential_file, is_secret_path


@pytest.mark.parametrize("text,secret", [
    ('{"api_key": "abcdef0123456789"}', "abcdef0123456789"),
    ('{"AGENT_WALLET_MASTER_SEED": "abcdef0123456789"}', "abcdef0123456789"),
    ('Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l', "YWxhZGRpbjpvcGVuc2VzYW1l"),
    ('https://alice:privatepassword@example.com/a', "privatepassword"),
    ('Cookie: session=privatecookie; csrf=privatecsrf', "privatecookie"),
    ('github_pat_abc123_DEF456_' + 'x' * 45, 'github_pat_abc123_DEF456_'),
])
def test_credential_shapes_do_not_survive_persistence(text, secret):
    assert secret not in scrub_secret_shapes(text)


@pytest.mark.parametrize("path", [
    "/home/user/.docker/config.json", "/home/user/.kube/config",
    "/home/user/.config/gh/hosts.yml", "/home/user/.config/solana/id.json",
    "/home/user/keystore/UTC--2026--wallet",
])
def test_external_credential_stores_are_denied(path):
    assert is_secret_path(Path(path), root=Path("/home/user"))
    assert is_credential_file(Path(path))


def test_unlabelled_bip39_mnemonic_is_redacted():
    phrase = " ".join(["abandon"] * 11 + ["about"])
    assert phrase not in scrub_secret_shapes("before: " + phrase + ". after")
    assert "abandon" not in scrub_secret_shapes(phrase.upper()).lower()
    ordinary = "This is a normal sentence about tests that should remain readable."
    assert scrub_secret_shapes(ordinary) == ordinary


def test_solana_secret_array_is_verified_before_redaction():
    import json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    seed = bytes(range(32))
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    secret = json.dumps(list(seed + public))
    assert secret not in scrub_secret_shapes(secret)
    ordinary = json.dumps(list(range(64)))
    assert scrub_secret_shapes(ordinary) == ordinary


def test_bad_mnemonic_checksum_does_not_redact_a_word_list():
    ordinary = " ".join(["abandon"] * 12)
    assert scrub_secret_shapes(ordinary) == ordinary
