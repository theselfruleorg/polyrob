"""The agent knows the version it RUNS and can read what shipped.

- `<environment>` / status show `core.version.get_version()` (+ the deploy's
  `.deployed_sha`), and the install marker is labelled "installed at".
- `release_notes()` reads the CHANGELOG.md next to the code, honestly absent.
- an unreadable self-context doc is NOT the same as an absent one.
"""
from pathlib import Path

import pytest

import core.version as v


@pytest.fixture
def code_root(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "_code_root", lambda: tmp_path)
    return tmp_path


def test_running_version_line_names_the_deployed_sha(code_root):
    assert v.running_version_line() == f"v{v.get_version()}"
    (code_root / ".deployed_sha").write_text("0123456789abcdef0123\n")
    assert v.deployed_sha() == "0123456789ab"
    assert v.running_version_line() == f"v{v.get_version()} (deployed 0123456789ab)"


def test_release_notes_absent_is_said_honestly(code_root):
    assert "not shipped with this install" in v.release_notes()


def test_release_notes_returns_one_section_capped(code_root, monkeypatch):
    monkeypatch.setattr(v, "get_version", lambda: "2.0.0")
    (code_root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- next\n\n"
        "## [1.1.0] — 2026-10-01\n\n### Fixed\n- " + "x" * 50 + "\n\n"
        "## [1.0.0] — 2026-09-01\n\n### Added\n- first\n")
    # running version has no section → newest released one
    latest = v.release_notes("latest")
    assert latest.startswith("## [1.1.0]") and "first" not in latest
    assert "first" in v.release_notes("v1.0.0")
    assert "next" in v.release_notes("unreleased")
    assert "Known: unreleased, 1.1.0, 1.0.0" in v.release_notes("9.9")
    assert "[truncated at 20 chars]" in v.release_notes("1.1.0", max_chars=20)


def test_the_real_changelog_has_the_running_version():
    assert f"## [{v.get_version()}]" in v.release_notes()


def test_install_lines_lead_with_the_running_version():
    from core.install_facts import install_lines
    lines = install_lines()
    assert lines[0].startswith(f"Running version: v{v.get_version()}")
    assert "release_notes" in lines[0]


def test_bootstrap_describe_labels_the_install_version(tmp_path):
    import core.bootstrap_marker as bm
    bm.write_marker(tmp_path, version="0.9.0", install_method="pipx")
    assert "installed at v0.9.0" in bm.describe(tmp_path)


def test_identity_section_reports_the_running_version(tmp_path):
    from core.status_snapshot import _identity_section
    sec = _identity_section("rob", str(tmp_path))
    assert sec.data["version"] == v.get_version()
    assert any(line.startswith(f"running: v{v.get_version()}") for line in sec.lines)


def test_session_metadata_uses_the_version_ssot():
    import logging
    from agents.task.agent.core.session_metadata import SessionMetadataMixin  # noqa
    obj = SessionMetadataMixin.__new__(SessionMetadataMixin)
    obj.logger = logging.getLogger("t")
    obj._set_version_and_source()
    assert obj.version == v.get_version()


def test_unreadable_self_context_doc_is_not_silent(tmp_path, caplog):
    from core.instance import load_self_context
    ident = tmp_path / "identity"
    ident.mkdir()
    # bytes that are not UTF-8 → read_text raises
    from core import instance
    name = instance._SELF_CONTEXT_DOCS[0]
    bad = ident / name
    bad.write_bytes(b"\xff\xfe\xfa not utf-8")
    with caplog.at_level("WARNING"):
        out = load_self_context(tmp_path)
    assert "UNREADABLE" in out and name in out
    assert any("UNREADABLE" in r.getMessage() for r in caplog.records)


def test_absent_self_context_stays_empty(tmp_path):
    from core.instance import load_self_context
    assert load_self_context(tmp_path) == ""


def test_the_deploy_ships_and_rolls_back_the_changelog():
    root = Path(__file__).resolve().parents[3]
    prod = (root / "scripts/deploy_prod.sh").read_text(encoding="utf-8")
    assert 'rsync -a "$TMP/CHANGELOG.md" "$APP_DIR/CHANGELOG.md"' in prod
    tx = (root / "scripts/deploy_transaction.sh").read_text(encoding="utf-8")
    assert tx.count("CHANGELOG.md") >= 3  # snapshot, restore, absent-ok


def test_unreadable_self_md_is_not_silent(tmp_path):
    from core.instance import load_self_doc, self_tier_root
    root = self_tier_root(tmp_path, "owner1", "rob")
    root.mkdir(parents=True)
    (root / "self.md").write_bytes(b"\xff\xfe not utf-8")
    out = load_self_doc(tmp_path, "owner1", "rob")
    assert "self.md is UNREADABLE" in out
    (root / "self.md").unlink()
    assert load_self_doc(tmp_path, "owner1", "rob") == ""


def test_release_notes_drop_trailing_link_definitions(tmp_path, monkeypatch):
    import core.version as v
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.1] — 2026-09-17\n\n### Fixed\n- a fix\n\n"
        "## [1.0.0] — 2026-09-16\n\n### Added\n- first\n\n"
        "[1.0.1]: https://example.com/1.0.1\n[1.0.0]: https://example.com/1.0.0\n")
    monkeypatch.setattr(v, "_code_root", lambda: tmp_path)
    notes = v.release_notes("1.0.0")
    assert "- first" in notes and "example.com" not in notes


def test_unreadable_note_is_not_exported_or_counted_loaded():
    from pathlib import Path
    from core.instance import _unreadable_note, is_unreadable_note
    note = _unreadable_note(Path("self.md"), PermissionError("x"))
    assert is_unreadable_note(note) and not is_unreadable_note("my notes")
