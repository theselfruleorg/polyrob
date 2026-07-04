"""C2 — grep/search pure-logic tests (regex, glob, gitignore-aware, bounded)."""
from tools.coding.search import search_files


def _seed(tmp_path):
    (tmp_path / "a.py").write_text("import os\nVALUE = 42\n")
    (tmp_path / "b.py").write_text("VALUE = 7\nother = 1\n")
    (tmp_path / "notes.txt").write_text("VALUE in prose\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("def f():\n    return VALUE\n")
    return tmp_path


def test_content_mode_finds_pattern_with_line_numbers(tmp_path):
    _seed(tmp_path)
    hits = search_files(str(tmp_path), r"VALUE = \d+", output_mode="content")
    found = {(h.path.split("/")[-1], h.line_no, h.line.strip()) for h in hits}
    assert ("a.py", 2, "VALUE = 42") in found
    assert ("b.py", 1, "VALUE = 7") in found


def test_files_mode_returns_unique_paths(tmp_path):
    _seed(tmp_path)
    files = search_files(str(tmp_path), "VALUE", output_mode="files")
    names = sorted(p.split("/")[-1] for p in files)
    assert names == ["a.py", "b.py", "c.py", "notes.txt"]


def test_glob_scopes_to_matching_files(tmp_path):
    _seed(tmp_path)
    files = search_files(str(tmp_path), "VALUE", glob="*.py", output_mode="files")
    assert all(p.endswith(".py") for p in files)
    assert not any(p.endswith("notes.txt") for p in files)


def test_respects_gitignore(tmp_path):
    _seed(tmp_path)
    (tmp_path / ".gitignore").write_text("notes.txt\nsub/\n")
    files = search_files(str(tmp_path), "VALUE", output_mode="files")
    names = sorted(p.split("/")[-1] for p in files)
    assert "notes.txt" not in names
    assert "c.py" not in names  # under ignored sub/
    assert names == ["a.py", "b.py"]


def test_always_skips_dot_git_and_pycache(tmp_path):
    _seed(tmp_path)
    g = tmp_path / ".git"
    g.mkdir()
    (g / "config").write_text("VALUE = secret\n")
    files = search_files(str(tmp_path), "VALUE", output_mode="files")
    assert not any("/.git/" in p or p.endswith("config") for p in files)


def test_bounded_output(tmp_path):
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("MATCH\n")
    hits = search_files(str(tmp_path), "MATCH", output_mode="content", max_results=10)
    assert len(hits) == 10
