"""A3 — context-reference path-traversal / SSRF guard.

`preprocess_context_references` is being wired into CLI/A2A intake, so user input
can now contain `@file:`/`@url:` tokens. Opt-in confinement (`confine_to_root=True`)
must refuse paths that escape the allowed root and URLs that target private/loopback
hosts. Default (confine off) preserves existing behaviour exactly.
"""
import os

from agents.task.agent.messages.context_references import (
    _is_safe_url,
    _is_within_root,
    preprocess_context_references,
)


# ---- pure path guard ------------------------------------------------------

def test_within_root_true_for_contained_path(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    assert _is_within_root(str(f), str(tmp_path)) is True


def test_within_root_false_for_parent_escape(tmp_path):
    sub = tmp_path / "ws"
    sub.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    # ../secret.txt relative to sub escapes sub
    escape = os.path.join(str(sub), "..", "secret.txt")
    assert _is_within_root(escape, str(sub)) is False


def test_within_root_false_for_absolute_outside(tmp_path):
    assert _is_within_root("/etc/hosts", str(tmp_path)) is False


# ---- pure URL guard -------------------------------------------------------

def test_safe_url_blocks_loopback_and_private():
    assert _is_safe_url("http://127.0.0.1/x") is False
    assert _is_safe_url("http://localhost/x") is False
    assert _is_safe_url("http://10.0.0.5/x") is False
    assert _is_safe_url("http://169.254.169.254/latest/meta-data") is False
    assert _is_safe_url("http://[::1]/x") is False


def test_safe_url_blocks_non_http_scheme():
    assert _is_safe_url("ftp://8.8.8.8/x") is False
    assert _is_safe_url("file:///etc/passwd") is False


def test_safe_url_allows_public_ip_literal():
    assert _is_safe_url("http://8.8.8.8/x") is True
    assert _is_safe_url("https://8.8.8.8/x") is True


# ---- integration through preprocess --------------------------------------

def test_confined_file_inside_root_expands(tmp_path):
    (tmp_path / "note.txt").write_text("ROOTED CONTENT")
    out = preprocess_context_references(
        "see @file:note.txt", root=str(tmp_path), confine_to_root=True
    )
    assert "ROOTED CONTENT" in out


def test_confined_file_escaping_root_is_refused(tmp_path):
    sub = tmp_path / "ws"
    sub.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    out = preprocess_context_references(
        "leak @file:../secret.txt", root=str(sub), confine_to_root=True
    )
    assert "TOP SECRET" not in out
    assert "refused" in out.lower()


def test_confined_private_url_is_refused():
    out = preprocess_context_references(
        "grab @url:http://169.254.169.254/latest", root=None, confine_to_root=True
    )
    assert "refused" in out.lower() or "blocked" in out.lower()


def test_default_unconfined_behaviour_unchanged(tmp_path):
    # Without confinement the escape still resolves (back-compat contract).
    # Note: filename must not match any SECRET_NAME_GLOBS (e.g. *secret*, .env*).
    sub = tmp_path / "ws"
    sub.mkdir()
    (tmp_path / "legacy.txt").write_text("LEGACY CONTENT")
    out = preprocess_context_references("x @file:../legacy.txt", root=str(sub))
    assert "LEGACY CONTENT" in out


def test_allow_filesystem_false_refuses_file_even_inside_root(tmp_path):
    # Remote (A2A) intake: filesystem refs are refused regardless of root containment.
    (tmp_path / "inside.txt").write_text("SERVER FILE")
    out = preprocess_context_references(
        "@file:inside.txt", root=str(tmp_path), confine_to_root=True,
        allow_filesystem=False,
    )
    assert "SERVER FILE" not in out
    assert "refused" in out.lower()


def test_allow_filesystem_false_still_blocks_diff_and_folder(tmp_path):
    (tmp_path / "d").mkdir()
    out = preprocess_context_references(
        "@folder:d and @diff", root=str(tmp_path), confine_to_root=True,
        allow_filesystem=False,
    )
    assert "refused" in out.lower()
    # @diff token should not have produced a git diff block
    assert "<context-ref" not in out


# ---- secret / binary guard (Task 3) ----------------------------------------

def test_secret_file_is_refused_and_bytes_absent(tmp_path):
    """@file pointing at a .env file must be refused; secret bytes must NOT appear."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=supersecret123\nANOTHER=topsecret")
    out = preprocess_context_references(
        f"@file:.env",
        root=str(tmp_path),
        confine_to_root=True,
    )
    assert "supersecret123" not in out
    assert "topsecret" not in out
    assert "refused" in out.lower() or "sensitive" in out.lower()


def test_secret_file_refused_even_when_unconfined(tmp_path):
    """Headline contract: a secret @file is refused even when confine_to_root=False.

    A secret is a secret regardless of confinement — the guard lives outside the
    `if confine_to_root:` branch.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=unconfinedsecret999")
    out = preprocess_context_references(
        "@file:.env",
        root=str(tmp_path),
        confine_to_root=False,
    )
    # (a) refusal note present
    assert "refused" in out.lower() or "sensitive" in out.lower()
    # (b) secret bytes absent
    assert "unconfinedsecret999" not in out


def test_binary_file_gives_metadata_note_and_no_raw_bytes(tmp_path):
    """@file pointing at a binary (null-byte) file must return a metadata note."""
    bin_file = tmp_path / "data.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03binary content\x00\xff")
    out = preprocess_context_references(
        f"@file:data.bin",
        root=str(tmp_path),
        confine_to_root=True,
    )
    # Raw null bytes must not appear in the output
    assert "\x00" not in out
    # Should contain a metadata note, not silently inline garbage
    assert "binary" in out.lower() or "not inlined" in out.lower()


def test_normal_file_still_inlines_content(tmp_path):
    """Normal @file (non-secret, non-binary) must still expand as before."""
    (tmp_path / "readme.txt").write_text("HELLO WORLD CONTENT")
    out = preprocess_context_references(
        "@file:readme.txt",
        root=str(tmp_path),
        confine_to_root=True,
    )
    assert "HELLO WORLD CONTENT" in out


def test_folder_listing_omits_secret_entries(tmp_path):
    """@folder listing must omit or redact .env files within the directory."""
    subdir = tmp_path / "workspace"
    subdir.mkdir()
    (subdir / ".env").write_text("SECRET=yes")
    (subdir / ".env.production").write_text("PROD_SECRET=yes")
    (subdir / "readme.txt").write_text("safe")
    out = preprocess_context_references(
        "@folder:workspace",
        root=str(tmp_path),
        confine_to_root=True,
    )
    # Safe file should appear; secret files must be absent or redacted
    assert "readme.txt" in out
    # .env should not appear as a plain entry (may be omitted or marked redacted)
    # We check that either the entry is absent OR marked redacted
    lines = out.split("\n")
    for line in lines:
        if ".env" in line.lower():
            assert "redact" in line.lower() or "secret" in line.lower(), (
                f"'.env' entry appeared in folder listing without redaction: {line!r}"
            )
