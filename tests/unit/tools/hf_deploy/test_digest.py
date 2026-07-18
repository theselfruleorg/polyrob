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


@pytest.mark.parametrize("skipdir", [".git", "coding_snapshots", "node_modules", "__pycache__"])
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
