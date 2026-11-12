"""M12: a private key has the same shape as a transaction hash (``0x`` + 64 hex).

The bare shape stays readable — the owner reads tx hashes in logs — but a value a
key-like name assigns is redacted.
"""
import pytest

from core.security_logging_filter import SecretScrubbingFilter

H = "0x" + "ab12" * 16


@pytest.fixture
def scrub():
    return SecretScrubbingFilter().scrub_message


@pytest.mark.parametrize("line", [
    f"key={H}",
    f"pk: {H}",
    f"PK={H}",
    f"private_key={H}",
    f"priv_key = {H}",
    f'{{"signer_key": "{H}"}}',
    f"privkey {H}",
    f"wallet key {H}",
    f"seed={H}",
    f"mnemonic: {H}",
    f"secret='{H}'",
])
def test_a_keyed_hex64_value_is_redacted(scrub, line):
    out = scrub(line)
    assert H not in out
    assert "ab12ab12ab12" not in out


@pytest.mark.parametrize("line", [
    f"tx sent {H}",
    f"swap confirmed {H} on base",
    f"tx_hash={H}",
    f"hash={H}",
    f"txid: {H}",
    f"transaction {H} mined",
    f"block {H}",
    f"topic0={H}",
    f"key_hash={H}",
    f"pubkey={H}",
    f"idempotency_key={H}",
    f"see https://basescan.org/tx/{H} now",
])
def test_a_transaction_hash_stays_readable(scrub, line):
    assert H in scrub(line)


def test_a_hash_and_a_key_on_one_line(scrub):
    out = scrub(f"tx={H} signer_key={'0x' + 'cd34' * 16}")
    assert H in out
    assert "cd34cd34" not in out
