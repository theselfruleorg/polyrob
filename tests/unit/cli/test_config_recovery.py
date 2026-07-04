"""TDD Task 17 — Corrupted-config recovery.

When cli.json is corrupt:
  - a .bak.<timestamp> file is written with the original bytes
  - a warning is emitted (stderr / logging)
  - load_cli_config() returns {}  (never raises)

Success path and missing-file path must be byte-identical to before.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cli.config_store import load_cli_config, save_cli_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CORRUPT_BYTES = b"{ not valid json"


def _set_config_path(monkeypatch, path: Path):
    """Route _config_path() to *path* via the env override."""
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(path))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCorruptRecovery:
    def test_corrupt_returns_empty(self, tmp_path, monkeypatch):
        """Corrupt file → {} (unchanged contract)."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        result = load_cli_config()

        assert result == {}

    def test_corrupt_creates_bak(self, tmp_path, monkeypatch):
        """Corrupt file → a .bak.* sibling is created."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        load_cli_config()

        baks = list(tmp_path.glob("cli.json.bak.*"))
        assert len(baks) == 1, f"expected 1 .bak file, got {baks}"

    def test_corrupt_bak_contains_original_bytes(self, tmp_path, monkeypatch):
        """The .bak file preserves the original corrupt content."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        load_cli_config()

        bak = next(tmp_path.glob("cli.json.bak.*"))
        assert bak.read_bytes() == CORRUPT_BYTES

    def test_corrupt_emits_warning(self, tmp_path, monkeypatch, caplog):
        """Corrupt file → warning logged (warning level)."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        with caplog.at_level(logging.WARNING, logger="cli.config_store"):
            load_cli_config()

        assert caplog.records, "expected at least one log record"
        record = caplog.records[0]
        assert record.levelno >= logging.WARNING
        # backup path name should appear in the message
        assert "bak" in record.message.lower() or "bak" in str(record.args).lower()

    def test_corrupt_original_is_moved_aside(self, tmp_path, monkeypatch):
        """After recovery the original corrupt path no longer exists (rename-aside)."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        load_cli_config()

        # The original file should be gone (renamed to .bak)
        assert not p.exists(), "original corrupt file should have been renamed aside"


class TestSuccessPathUnchanged:
    def test_valid_config_returned(self, tmp_path, monkeypatch):
        """Valid JSON → returns dict, no .bak created."""
        p = tmp_path / "cli.json"
        payload = {"default_provider": "openai", "default_model": "gpt-5"}
        p.write_text(json.dumps(payload))
        _set_config_path(monkeypatch, p)

        result = load_cli_config()

        assert result == payload
        assert list(tmp_path.glob("cli.json.bak.*")) == []

    def test_non_dict_json_returns_empty_no_bak(self, tmp_path, monkeypatch):
        """JSON that is not a dict (e.g. a list) → {} without .bak (same as before)."""
        p = tmp_path / "cli.json"
        p.write_text("[1, 2, 3]")
        _set_config_path(monkeypatch, p)

        result = load_cli_config()

        assert result == {}
        # non-dict is valid JSON but wrong shape — no bak expected
        assert list(tmp_path.glob("cli.json.bak.*")) == []


class TestMissingFileUnchanged:
    def test_missing_returns_empty_no_bak(self, tmp_path, monkeypatch):
        """Missing file → {}, no .bak, no warning."""
        p = tmp_path / "cli.json"  # does not exist
        _set_config_path(monkeypatch, p)

        result = load_cli_config()

        assert result == {}
        assert list(tmp_path.glob("cli.json.bak.*")) == []


class TestBackupFailureSafety:
    def test_backup_failure_still_returns_empty(self, tmp_path, monkeypatch):
        """If the rename/copy fails, load_cli_config() must NOT raise — returns {}."""
        import cli.config_store as cs

        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        # Simulate os.rename (or Path.rename) raising
        original_rename = Path.rename

        def _fail_rename(self, target):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(Path, "rename", _fail_rename)

        # Must not raise
        result = load_cli_config()
        assert result == {}

    def test_subsequent_save_works_after_recovery(self, tmp_path, monkeypatch):
        """After corrupt recovery, save_cli_config can write a fresh file."""
        p = tmp_path / "cli.json"
        p.write_bytes(CORRUPT_BYTES)
        _set_config_path(monkeypatch, p)

        load_cli_config()  # moves corrupt file aside

        # Now save fresh config
        save_cli_config({"default_provider": "anthropic"})

        # Fresh file should be valid
        result = load_cli_config()
        assert result == {"default_provider": "anthropic"}
