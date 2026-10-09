"""SUP-8: credential shapes the shared battery and the log filter used to miss."""
import json
import logging

import pytest

from core.secret_scrub import scrub_secret_shapes
from core.security_logging_filter import SecretScrubbingFilter

_PHRASE = ["abandon"] * 11 + ["about"]  # a checksum-valid BIP-39 phrase


def _keypair_json():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat)
    k = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw = (k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
           + k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return json.dumps(list(raw))


CASES = {
    "mcp_key": ("MCP_ENCRYPTION_KEY=" + "Zx81kQ" "2mT9vL0pPq7rS3tU5wX6yA1bC4dE7fG0hJ=", "Zx81kQ2m"),
    "jwt_secret": ("JWT_SECRET_KEY=hunter2hunter2longvalue", "hunter2"),
    "aws_secret": ("AWS_SECRET_ACCESS_KEY=" + "wJalrX" "UtnFEMI/K7MDENG/bPxRfiCY", "wJalrXUtn"),
    "b64_suffix": ("TWITTER_CHAT_PRIVATE_KEYS_B64=" + "QUJDRE" "VGR0hJSktMTU5PUA==", "QUJDREVG"),
    "payment_seed": ("PAYMENT_MASTER_SEED=deadbeefcafebabe0011", "deadbeefcafe"),
    "postgres": ("connecting postgres://admin:S3cretPw@db.internal:5432/app", "S3cretPw"),
    "https_userinfo": ("fetch https://user:pa55word@example.com/x failed", "pa55word"),
    "numbered_phrase": ("seed: " + " ".join(f"{i + 1}. {w}" for i, w in enumerate(_PHRASE)),
                        "12. about"),
    "bare_phrase": ("recovered " + " ".join(_PHRASE) + " ok", "abandon abandon"),
    "cookie": ("Cookie: session=abcdef123456", "abcdef123456"),
    "keypair": ("loaded " + _keypair_json(), "[0, 1, 2"),
    "telegram_url": ("Cannot connect to host https://api.telegram.org/"
                     "bot123456789:AAH-dq_TcvCH1vG-WJxf_SeofSAs0K5PAL/getUpdates", "AAH-dq_"),
    "stripe": ("stripe sk_" + "live_" + "51HabcDEFghiJKL", "51HabcDEF"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_shared_scrubber_redacts(name):
    text, secret = CASES[name]
    assert secret not in scrub_secret_shapes(text)


@pytest.mark.parametrize("name", sorted(CASES))
def test_log_filter_redacts(name):
    text, secret = CASES[name]
    record = logging.LogRecord("t", logging.INFO, "p", 1, text, None, None)
    SecretScrubbingFilter().filter(record)
    assert secret not in record.getMessage()


@pytest.mark.parametrize("line", [
    "GET /api/status 200 in 12ms",
    "goal 42 finished with status done",
    "ssh://git@github.com/org/repo.git cloned",
    "the agent did not find any goal to run so it went back to sleep for now ok",
    "pk_live_51HabcDEFghiJKL is the publishable key",
])
def test_ordinary_lines_survive(line):
    assert scrub_secret_shapes(line) == line
    record = logging.LogRecord("t", logging.INFO, "p", 1, line, None, None)
    SecretScrubbingFilter().filter(record)
    assert record.getMessage() == line


def test_precheck_is_linear_on_long_prose():
    import time
    start = time.monotonic()
    SecretScrubbingFilter._MARKERLESS_PRECHECK.search("ab, " * 50000)
    assert time.monotonic() - start < 1.0
