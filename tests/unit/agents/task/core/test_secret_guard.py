"""Tests for agents.task.agent.core.secret_guard — secret/binary path guard."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.task.agent.core.secret_guard import (
    SECRET_DIR_PARTS,
    SECRET_NAME_GLOBS,
    estimate_tokens_rough,
    is_binary_file,
    is_protected_config_path,
    is_secret_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path("/project")


def p(rel: str) -> Path:
    """Shorthand: make an absolute path from a relative string."""
    return ROOT / rel


# ---------------------------------------------------------------------------
# is_secret_path — TRUE cases
# ---------------------------------------------------------------------------


class TestIsSecretPathTrue:
    def test_dotenv_file(self):
        assert is_secret_path(p(".env"), root=ROOT) is True

    def test_dotenv_local(self):
        assert is_secret_path(p(".env.local"), root=ROOT) is True

    def test_id_ed25519(self):
        assert is_secret_path(p("id_ed25519"), root=ROOT) is True

    def test_id_rsa(self):
        assert is_secret_path(p("id_rsa"), root=ROOT) is True

    def test_id_generic(self):
        # matches ``id_*``
        assert is_secret_path(p("id_ecdsa"), root=ROOT) is True

    def test_pem(self):
        assert is_secret_path(p("server.pem"), root=ROOT) is True

    def test_key(self):
        assert is_secret_path(p("private.key"), root=ROOT) is True

    def test_p12(self):
        assert is_secret_path(p("cert.p12"), root=ROOT) is True

    def test_pfx(self):
        assert is_secret_path(p("cert.pfx"), root=ROOT) is True

    def test_netrc(self):
        assert is_secret_path(p(".netrc"), root=ROOT) is True

    def test_npmrc(self):
        assert is_secret_path(p(".npmrc"), root=ROOT) is True

    def test_pypirc(self):
        assert is_secret_path(p(".pypirc"), root=ROOT) is True

    def test_pgpass(self):
        assert is_secret_path(p(".pgpass"), root=ROOT) is True

    def test_bot_db(self):
        assert is_secret_path(p("bot.db"), root=ROOT) is True

    def test_credential_in_name(self):
        assert is_secret_path(p("aws_credentials"), root=ROOT) is True

    def test_secret_in_name(self):
        assert is_secret_path(p("my_secret_key.json"), root=ROOT) is True

    # polyrob-specific patterns
    def test_config_dotenv_production(self):
        assert is_secret_path(p("config/.env.production"), root=ROOT) is True

    def test_config_dotenv_development(self):
        assert is_secret_path(p("config/.env.development"), root=ROOT) is True

    def test_rob_dotenv(self):
        assert is_secret_path(p(".rob/.env"), root=ROOT) is True

    # sibling-gap fix (2026-07-16): the M2 snapshot globs (added only to
    # CREDENTIAL_NAME_GLOBS) must also be in SECRET_NAME_GLOBS, since
    # is_secret_path is the guard consumed by KB ingest / context-references /
    # project-context — a MASTER_SEED-holding `.env.production` copy under a
    # `polyrob update` snapshot must not be readable into model context either.
    def test_snapshot_config_copy_is_secret(self):
        assert is_secret_path(
            p("snapshots/20260715T120000Z_0.5.1/config/00_.env.production"), root=ROOT
        ) is True

    def test_snapshot_dirs_copy_is_secret(self):
        assert is_secret_path(
            p("snapshots/20260715T120000Z_0.5.1/dirs/00_wallet/meta.json"), root=ROOT
        ) is True

    def test_env_dotted_glob_is_secret(self):
        assert is_secret_path(p("00_.env.production"), root=ROOT) is True

    def test_wallet_audit_jsonl_is_secret(self):
        assert is_secret_path(p("wallet/audit.jsonl"), root=ROOT) is True

    def test_wallet_audit_jsonl_hwm_sidecar_is_secret(self):
        assert is_secret_path(p("wallet/audit.jsonl.hwm"), root=ROOT) is True

    # *.db inside a `data/` parent
    def test_db_under_data(self):
        assert is_secret_path(p("data/bot.db"), root=ROOT) is True

    def test_db_deep_under_data(self):
        assert is_secret_path(p("data/subdir/sessions.db"), root=ROOT) is True

    # directory-part checks
    def test_file_under_ssh(self):
        assert is_secret_path(p(".ssh/known_hosts"), root=ROOT) is True

    def test_file_under_aws(self):
        assert is_secret_path(p(".aws/credentials"), root=ROOT) is True

    def test_file_under_gnupg(self):
        assert is_secret_path(p(".gnupg/secring.gpg"), root=ROOT) is True

    def test_file_under_kube(self):
        assert is_secret_path(p(".kube/config"), root=ROOT) is True

    def test_file_under_docker(self):
        assert is_secret_path(p(".docker/config.json"), root=ROOT) is True

    def test_file_under_azure(self):
        assert is_secret_path(p(".azure/accessTokens.json"), root=ROOT) is True

    def test_file_under_config_gh(self):
        assert is_secret_path(p(".config/gh/hosts.yml"), root=ROOT) is True

    def test_file_under_data_dir(self):
        # `data` is in SECRET_DIR_PARTS → any file under data/ is secret
        assert is_secret_path(p("data/README.md"), root=ROOT) is True

    def test_case_insensitive_basename(self):
        # glob matching is case-insensitive
        assert is_secret_path(p("ID_RSA"), root=ROOT) is True

    def test_case_insensitive_dotenv(self):
        assert is_secret_path(p(".ENV"), root=ROOT) is True


# ---------------------------------------------------------------------------
# is_secret_path — FALSE cases
# ---------------------------------------------------------------------------


class TestIsSecretPathFalse:
    def test_readme(self):
        assert is_secret_path(p("README.md"), root=ROOT) is False

    def test_src_app_py(self):
        assert is_secret_path(p("src/app.py"), root=ROOT) is False

    def test_requirements_txt(self):
        assert is_secret_path(p("requirements.txt"), root=ROOT) is False

    def test_main_py(self):
        assert is_secret_path(p("main.py"), root=ROOT) is False

    def test_config_json_not_under_data(self):
        # a .json file in config/ without secret globs matching
        assert is_secret_path(p("config/settings.json"), root=ROOT) is False

    def test_db_not_under_data(self):
        # *.db is only secret when parent dir is named `data`
        assert is_secret_path(p("sessions.db"), root=ROOT) is False

    def test_db_under_databases_not_data(self):
        assert is_secret_path(p("databases/app.db"), root=ROOT) is False

    def test_ordinary_yaml(self):
        assert is_secret_path(p("docker-compose.yml"), root=ROOT) is False


# ---------------------------------------------------------------------------
# is_binary_file
# ---------------------------------------------------------------------------


class TestIsBinaryFile:
    def test_py_file_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "hello.py"
        f.write_text("print('hello')\n", encoding="utf-8")
        assert is_binary_file(f) is False

    def test_txt_file_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "notes.txt"
        f.write_text("some notes\n", encoding="utf-8")
        assert is_binary_file(f) is False

    def test_md_file_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "README.md"
        f.write_text("# Hello\n", encoding="utf-8")
        assert is_binary_file(f) is False

    def test_json_file_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}\n', encoding="utf-8")
        assert is_binary_file(f) is False

    def test_null_byte_content_is_binary(self, tmp_path: Path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"start\x00end")
        assert is_binary_file(f) is True

    def test_png_extension_is_binary(self, tmp_path: Path):
        # PNG magic bytes + null bytes
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        assert is_binary_file(f) is True

    def test_gif_with_null_bytes_is_binary(self, tmp_path: Path):
        f = tmp_path / "anim.gif"
        f.write_bytes(b"GIF89a\x00\x00")
        assert is_binary_file(f) is True

    def test_pdf_is_not_binary(self, tmp_path: Path):
        # .pdf is extractable — callers decide; we return False
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4\x00binary")
        assert is_binary_file(f) is False

    def test_docx_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "doc.docx"
        f.write_bytes(b"PK\x03\x04some binary data\x00\x00")
        assert is_binary_file(f) is False

    def test_pure_text_no_extension_is_not_binary(self, tmp_path: Path):
        f = tmp_path / "noext"
        f.write_bytes(b"just plain text here no nulls")
        assert is_binary_file(f) is False

    def test_null_byte_in_first_4096_is_binary(self, tmp_path: Path):
        # Null byte right at boundary
        payload = b"A" * 4095 + b"\x00"
        f = tmp_path / "boundary.bin"
        f.write_bytes(payload)
        assert is_binary_file(f) is True

    def test_null_byte_beyond_4096_not_detected(self, tmp_path: Path):
        # Null byte past the 4096-byte sniff window is NOT detected
        payload = b"A" * 4097 + b"\x00"
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        assert is_binary_file(f) is False


# ---------------------------------------------------------------------------
# estimate_tokens_rough
# ---------------------------------------------------------------------------


class TestEstimateTokensRough:
    def test_40_chars_gives_10(self):
        assert estimate_tokens_rough("a" * 40) == 10

    def test_four_chars_gives_1(self):
        assert estimate_tokens_rough("abcd") == 1

    def test_empty_string_returns_1(self):
        # max(1, ...) ensures we never return 0
        assert estimate_tokens_rough("") == 1

    def test_single_char_returns_1(self):
        assert estimate_tokens_rough("x") == 1

    def test_large_text(self):
        text = "x" * 1000
        assert estimate_tokens_rough(text) == 250

    def test_non_ascii(self):
        # Unicode chars still counted by len()
        text = "é" * 8
        assert estimate_tokens_rough(text) == 2


# ---------------------------------------------------------------------------
# is_protected_config_path
# ---------------------------------------------------------------------------


class TestIsProtectedConfigPath:
    def test_system_config_dir_etc_polyrob(self):
        # Files under /etc/polyrob are protected
        assert is_protected_config_path(Path("/etc/polyrob/polyrob.env")) is True

    def test_system_config_dir_etc_rob(self):
        # Files under /etc/rob are protected
        assert is_protected_config_path(Path("/etc/rob/config.yml")) is True

    def test_preferences_and_contract_are_protected(self):
        # Preferences and contract files under identity/ are protected
        assert is_protected_config_path(Path("/data/identity/rob/user_1/preferences.toml")) is True
        assert is_protected_config_path(Path("/data/identity/rob/user_1/contract.md")) is True

    def test_preferences_anywhere_under_identity_path(self):
        # Nested deeper in identity/ path
        assert is_protected_config_path(Path("/some/path/identity/instance/user_123/preferences.toml")) is True

    def test_contract_anywhere_under_identity_path(self):
        # Contract files under identity/ path
        assert is_protected_config_path(Path("/home/rob/identity/rob/user_abc/contract.md")) is True

    def test_preferences_not_under_identity_not_protected(self):
        # preferences.toml NOT under identity/ should not be protected
        assert is_protected_config_path(Path("/data/preferences.toml")) is False

    def test_contract_not_under_identity_not_protected(self):
        # contract.md NOT under identity/ should not be protected
        assert is_protected_config_path(Path("/some/path/contract.md")) is False

    def test_other_files_under_identity_not_protected(self):
        # Other files under identity/ are OK (only preferences.toml and contract.md)
        assert is_protected_config_path(Path("/data/identity/rob/user_1/README.md")) is False
        assert is_protected_config_path(Path("/data/identity/rob/user_1/data.json")) is False

    # -- review-fix regressions: case-insensitivity + segment robustness --

    def test_mixed_case_identity_segment_is_protected(self):
        # Case-insensitive filesystems (macOS APFS, Windows) treat `Identity/`
        # as the same directory as `identity/` — must still be protected.
        assert is_protected_config_path(Path("/data/Identity/rob/user_1/preferences.toml")) is True
        assert is_protected_config_path(Path("/data/IDENTITY/rob/user_1/contract.md")) is True

    def test_relative_path_no_leading_slash_is_protected(self):
        # Bare relative paths (no leading slash) must still be caught.
        assert is_protected_config_path(Path("identity/u/preferences.toml")) is True
        assert is_protected_config_path(Path("identity/u/contract.md")) is True

    def test_mixed_case_relative_path_is_protected(self):
        assert is_protected_config_path(Path("Identity/u/preferences.toml")) is True

    def test_nonidentity_segment_not_protected(self):
        # A segment that merely CONTAINS "identity" as a substring (not an
        # exact segment match) must NOT be protected.
        assert is_protected_config_path(Path("/data/nonidentity/rob/user_1/preferences.toml")) is False
        assert is_protected_config_path(Path("/data/identityx/rob/user_1/contract.md")) is False


# ---------------------------------------------------------------------------
# Constant shape checks
# ---------------------------------------------------------------------------


def test_secret_name_globs_is_tuple():
    assert isinstance(SECRET_NAME_GLOBS, tuple)
    assert len(SECRET_NAME_GLOBS) > 0


def test_secret_dir_parts_is_frozenset():
    assert isinstance(SECRET_DIR_PARTS, frozenset)
    assert "data" in SECRET_DIR_PARTS
    assert ".ssh" in SECRET_DIR_PARTS
