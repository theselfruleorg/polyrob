"""M13 — raw EVM keys (with a key label), Solana 64-byte secrets, Telegram bot
tokens and Google API keys are redacted by BOTH scrubbers; tx hashes and
signatures in ordinary text survive."""
import logging

import pytest

from core.secret_patterns import REDACTED, apply_ssot_shapes
from core.secret_scrub import scrub_secret_shapes
from core.security_logging_filter import SecretScrubbingFilter

HEX64 = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
SOL_SECRET = "5J3mBbAH58CpQ3Y5RNJpUKPE62SQ5tfcvU2JpbnkeyhfsYB1Jcn5cYs1H2vp4QpLf9vS5nVbTP3sVhD8dG7kQxZR"  # gitleaks:allow (fake fixture)
TG = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawz"
GKEY = "AIzaSyA1234567890abcdefghijklmnopqrstuv"  # gitleaks:allow (fake fixture)


def _log(msg):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    SecretScrubbingFilter().filter(rec)
    return rec.getMessage()


SCRUBBERS = [apply_ssot_shapes, scrub_secret_shapes, _log]


@pytest.mark.parametrize("scrub", SCRUBBERS)
@pytest.mark.parametrize("text,secret", [
    (f"Private key: 0x{HEX64}", HEX64),
    (f"private key {HEX64}", HEX64),
    (f"the signing key is 0x{HEX64}", HEX64),
    (f"wallet_key = '0x{HEX64}'", HEX64),
    (f"imported secret {SOL_SECRET}", SOL_SECRET),
    (f"https://api.telegram.org/bot{TG}/sendMessage", TG),
    (f"TELEGRAM {TG}", TG),
    (f"https://maps.googleapis.com/api?key={GKEY}&q=x", GKEY),
])
def test_new_shapes_are_redacted(scrub, text, secret):
    out = scrub(text)
    assert secret not in out
    assert "redacted" in out


# The logging filter layers its own long-run catch-all on top (pre-existing), so
# "survives" is asserted for the two content scrubbers that persist history.
@pytest.mark.parametrize("scrub", [apply_ssot_shapes, scrub_secret_shapes])
@pytest.mark.parametrize("text", [
    f"tx 0x{HEX64} confirmed in block 123",
    f"https://basescan.org/tx/0x{HEX64}",
    f"transaction hash: 0x{HEX64}",
    f"storage slot 0x{HEX64} read",
    f"  sig: {SOL_SECRET}",
    f"signature {SOL_SECRET}",
    f"https://solscan.io/tx/{SOL_SECRET}",
    "token: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "mint 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
    "the key idea is 1234567890",
])
def test_hashes_signatures_and_addresses_survive(scrub, text):
    assert scrub(text) == text


def test_label_is_kept():
    assert apply_ssot_shapes(f"Private key: 0x{HEX64}") == f"Private key: {REDACTED}"
