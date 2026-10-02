"""CLI3 + CLI15 (2026-10-03 audit): the REPL history file never holds a secret.

CLI3: ``/config set HF_TOKEN …`` (and every other key ``core.secrets`` names a
secret) was saved in plaintext, in a 0644 file. CLI15: an EVM private key —
``0x`` + 64 hex — has the shape of a transaction hash, so it passed both the
display scrubber and the history filter.
"""
import os
import stat

import pytest

from cli.ui.history import TerminalHistory
from cli.ui.secrets import REDACTED, scrub_secrets

PK = "0x" + "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def _stored(tmp_path, *lines):
    path = tmp_path / "history"
    h = TerminalHistory(str(path))
    for line in lines:
        h.store_string(line)
    return list(TerminalHistory(str(path)).load_history_strings()), path


@pytest.mark.parametrize("key", ["HF_TOKEN", "GITHUB_TOKEN", "API_AUTH_TOKEN",
                                 "MCP_ENCRYPTION_KEY", "hf_token"])
def test_config_set_of_a_secret_key_is_not_saved(tmp_path, key):
    rows, _ = _stored(tmp_path, f"/config set {key} abc123", "hello")
    assert rows == ["hello"]


def test_config_set_of_a_plain_key_is_saved(tmp_path):
    rows, _ = _stored(tmp_path, "/config set approvals.provider cli")
    assert rows == ["/config set approvals.provider cli"]


def test_history_file_is_owner_only(tmp_path):
    _, path = _stored(tmp_path, "hello")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_existing_world_readable_history_is_tightened(tmp_path):
    path = tmp_path / "history"
    path.write_text("")
    os.chmod(path, 0o644)
    TerminalHistory(str(path)).store_string("hello")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_private_key_line_is_not_saved(tmp_path):
    rows, _ = _stored(tmp_path, f"import this wallet {PK}", "hello")
    assert rows == ["hello"]


def test_scrubber_redacts_a_labelled_private_key():
    out = scrub_secrets(f"private key: {PK}")
    assert PK not in out and REDACTED in out
    out = scrub_secrets(f"pk {PK}")
    assert PK not in out


def test_scrubber_keeps_a_tx_hash():
    assert scrub_secrets(f"tx: {PK}") == f"tx: {PK}"
