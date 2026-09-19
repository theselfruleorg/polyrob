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


# --- 2026-09-19 (Rob's self-review ask #2): a FILE path is searched, not walked ---
# `os.walk` on a file yields nothing, so `coding_grep(path="data/x-targets/rounds.md")`
# answered "(no matches)" for a line that IS there (2 wasted steps + a false
# "gitignored?" conclusion). A file path now greps that file; a path that is neither
# a file nor a directory raises, naming it — never a silent empty list.
def test_file_path_is_searched_directly(tmp_path):
    _seed(tmp_path)
    hits = search_files(str(tmp_path / "notes.txt"), "VALUE")
    assert [h.line for h in hits] == ["VALUE in prose"]
    assert hits[0].path == str(tmp_path / "notes.txt")


def test_file_path_files_mode(tmp_path):
    _seed(tmp_path)
    assert search_files(str(tmp_path / "a.py"), "VALUE", output_mode="files") == [str(tmp_path / "a.py")]


def test_file_path_ignores_glob_mismatch_of_its_own_name(tmp_path):
    """An explicit file is what the caller asked for; the glob scopes a WALK."""
    _seed(tmp_path)
    hits = search_files(str(tmp_path / "notes.txt"), "VALUE", glob="*.py")
    assert len(hits) == 1


def test_missing_path_raises_naming_it(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError) as ei:
        search_files(str(tmp_path / "nope.md"), "x")
    assert "nope.md" in str(ei.value)
