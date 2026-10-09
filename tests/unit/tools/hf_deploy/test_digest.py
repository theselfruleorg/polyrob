"""Workspace digest + the ship==tested green-test gate (acceptance-contract leg 1)."""
import pytest


def _digest(root):
    from tools.hf_deploy.digest import compute_workspace_digest
    return compute_workspace_digest(str(root))


def test_digest_is_deterministic(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    assert _digest(tmp_path) == _digest(tmp_path)


def test_digest_changes_when_content_changes(tmp_path):
    (tmp_path / "app.py").write_text("v1")
    d1 = _digest(tmp_path)
    (tmp_path / "app.py").write_text("v2")
    assert _digest(tmp_path) != d1


def test_digest_changes_when_file_added_or_renamed(tmp_path):
    (tmp_path / "a.py").write_text("x")
    d1 = _digest(tmp_path)
    (tmp_path / "b.py").write_text("y")
    d2 = _digest(tmp_path)
    assert d2 != d1
    (tmp_path / "b.py").rename(tmp_path / "c.py")
    assert _digest(tmp_path) != d2


@pytest.mark.parametrize("skipdir", [".git", "coding_snapshots", "__pycache__",
                                    ".pytest_cache", ".mypy_cache", ".tox", ".venv", "venv",
                                    ".pylibs"])
def test_digest_skips_noise_dirs(tmp_path, skipdir):
    (tmp_path / "app.py").write_text("x")
    d1 = _digest(tmp_path)
    noisy = tmp_path / skipdir
    noisy.mkdir()
    (noisy / "junk").write_text("churn")
    assert _digest(tmp_path) == d1


def test_digest_skips_noise_dirs_nested(tmp_path):
    (tmp_path / "app.py").write_text("x")
    d1 = _digest(tmp_path)
    nested = tmp_path / "pkg" / "__pycache__"
    nested.mkdir(parents=True)
    (nested / "mod.pyc").write_bytes(b"\x00")
    d2 = _digest(tmp_path)
    # pkg/ itself is new (empty dirs don't hash) but its __pycache__ content must not
    assert d2 == d1


# --- tested_tree_digest (the green-test gate) --------------------------------

def _gate(orch, root):
    from tools.hf_deploy.digest import tested_tree_digest
    return tested_tree_digest(orch, str(root))


def test_gate_passes_on_green_untouched_tree(tmp_path, green_orch):
    (tmp_path / "app.py").write_text("x")
    digest, reason = _gate(green_orch, tmp_path)
    assert reason is None
    assert digest == _digest(tmp_path)


def test_gate_refuses_when_no_orchestrator(tmp_path):
    digest, reason = _gate(None, tmp_path)
    assert digest is None
    assert "cannot verify" in reason.lower()


def test_gate_refuses_when_no_green_run_tests(tmp_path, no_green_orch):
    digest, reason = _gate(no_green_orch, tmp_path)
    assert digest is None
    assert "run_tests" in reason


def test_gate_refuses_when_edited_after_last_green_test(tmp_path, edited_after_test_orch):
    digest, reason = _gate(edited_after_test_orch, tmp_path)
    assert digest is None
    assert "edited" in reason.lower()


def test_digest_covers_what_a_deploy_actually_ships(tmp_path):
    """ONE exclusion policy (core/ship_tree.py). node_modules SHIPS (a read-only
    container cannot install anything), so it must be hashed; a symlink and a
    credential-shaped file never ship, so they must not be."""
    import os
    from core.ship_tree import SKIP_DIRS
    from tools.hf_deploy.digest import _SKIP_DIRS
    assert _SKIP_DIRS is SKIP_DIRS and "node_modules" not in SKIP_DIRS
    (tmp_path / "app.py").write_text("x")
    d1 = _digest(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("y")
    assert _digest(tmp_path) != d1
    d2 = _digest(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1")          # refused by the snapshot
    os.symlink("/etc/hostname", tmp_path / "link")      # refused by the snapshot
    assert _digest(tmp_path) == d2


def test_gate_refuses_a_test_entry_with_no_result(tmp_path):
    # Codex review 2026-09-25: a run_tests cut off before it returned (no
    # result) counted as green. A missing result proves nothing.
    from tests.unit.tools.hf_deploy.conftest import GreenLedgerOrch
    orch = GreenLedgerOrch([("coding_str_replace", None), ("coding_run_tests", None)])
    orch.agents["a1"].history.history[-1].result = []
    (tmp_path / "app.py").write_text("x")
    digest, reason = _gate(orch, tmp_path)
    assert digest is None and "run_tests" in reason


# --- ship == tested by CONTENT (harness review G4) -----------------------
# The ledger rule sees only the coding edit verbs. A shell `sed -i` (host shell,
# a background job, a run_code cell) after a green run_tests used to ship an
# untested tree. run_tests now records the tree it finished against.

def _gate_sid(orch, root, sid="sess-g4"):
    from tools.hf_deploy.digest import tested_tree_digest
    return tested_tree_digest(orch, str(root), session_id=sid)


def test_gate_refuses_a_shell_edit_after_green_test(tmp_path, green_orch):
    import os
    from core.ship_tree import record_tested_tree
    (tmp_path / "app.py").write_text("v1")
    record_tested_tree("sess-g4", str(tmp_path))
    assert _gate_sid(green_orch, tmp_path)[1] is None
    (tmp_path / "app.py").write_text("v2-from-sed")
    st = os.stat(tmp_path / "app.py")
    os.utime(tmp_path / "app.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    digest, reason = _gate_sid(green_orch, tmp_path)
    assert digest is None and "app.py" in reason and "re-run tests" in reason


def test_gate_refuses_added_and_removed_files(tmp_path, green_orch):
    from core.ship_tree import record_tested_tree
    (tmp_path / "a.py").write_text("a")
    record_tested_tree("sess-g4", str(tmp_path))
    (tmp_path / "a.py").unlink()
    (tmp_path / "b.py").write_text("b")
    reason = _gate_sid(green_orch, tmp_path)[1]
    assert "2 file(s)" in reason and "a.py" in reason and "b.py" in reason


def test_gate_compares_a_subdir_ship_against_the_tested_root(tmp_path, green_orch):
    from core.ship_tree import record_tested_tree
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x")
    (tmp_path / "notes.md").write_text("n")
    record_tested_tree("sess-g4", str(tmp_path))
    (tmp_path / "notes.md").write_text("changed outside the shipped dir")
    assert _gate_sid(green_orch, tmp_path / "app")[1] is None
    (tmp_path / "app" / "new.py").write_text("y")
    assert "app/new.py" in _gate_sid(green_orch, tmp_path / "app")[1]


def test_gate_keeps_ledger_rule_without_a_record(tmp_path, green_orch):
    (tmp_path / "app.py").write_text("x")
    assert _gate_sid(green_orch, tmp_path, sid="never-tested")[1] is None
