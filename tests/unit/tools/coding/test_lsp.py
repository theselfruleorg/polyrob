"""tools/coding/lsp.py — LSP diagnostics-after-edit (I-2 / H1).

Pure, fail-open, errors-only. The ``runner`` is always injected — these tests
never exec a real ``pyright``/``tsc`` binary.
"""
import json
import subprocess

from tools.coding.lsp import MAX_DIAGNOSTICS_CHARS, diagnose_file


class _FakeProc:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


def _pyright_json(errors=1, warnings=1):
    diagnostics = []
    for i in range(errors):
        diagnostics.append({
            "file": "x.py",
            "severity": "error",
            "message": f"undefined name 'foo{i}'",
            "range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 3}},
        })
    for i in range(warnings):
        diagnostics.append({
            "file": "x.py",
            "severity": "warning",
            "message": f"unused import bar{i}",
            "range": {"start": {"line": i + 100, "character": 0}, "end": {"line": i + 100, "character": 3}},
        })
    return json.dumps({"generalDiagnostics": diagnostics})


# --- pyright (.py) -----------------------------------------------------------

def test_pyright_one_error_one_warning_only_error_appears():
    def runner(cmd, cwd, timeout_sec):
        return _FakeProc(stdout=_pyright_json(errors=1, warnings=1))

    out = diagnose_file("x.py", "/root", runner=runner)
    assert out == "x.py:1:1 undefined name 'foo0'"
    assert "unused import" not in out


def test_pyright_clean_output_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        return _FakeProc(stdout=_pyright_json(errors=0, warnings=0))

    assert diagnose_file("x.py", "/root", runner=runner) == ""


def test_pyright_unparsable_json_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        return _FakeProc(stdout="not json at all")

    assert diagnose_file("x.py", "/root", runner=runner) == ""


# --- tsc (.ts/.tsx/.js/.jsx) --------------------------------------------------

def test_tsc_error_line_parsed():
    def runner(cmd, cwd, timeout_sec):
        stdout = (
            "src/x.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            "Found 1 error in src/x.ts:5\n"
        )
        return _FakeProc(stdout=stdout)

    out = diagnose_file("x.ts", "/root", runner=runner)
    assert out == "src/x.ts:3:5 TS2322: Type 'string' is not assignable to type 'number'."


def test_tsc_clean_output_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        return _FakeProc(stdout="Found 0 errors.\n")

    assert diagnose_file("x.tsx", "/root", runner=runner) == ""


# --- fail-open failure modes ---------------------------------------------------

def test_missing_checker_file_not_found_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        raise FileNotFoundError("pyright not found on PATH")

    assert diagnose_file("x.py", "/root", runner=runner) == ""


def test_timeout_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        raise subprocess.TimeoutExpired(cmd=["pyright"], timeout=timeout_sec)

    assert diagnose_file("x.py", "/root", timeout_sec=1.0, runner=runner) == ""


def test_arbitrary_exception_returns_empty():
    def runner(cmd, cwd, timeout_sec):
        raise ValueError("boom")

    assert diagnose_file("x.py", "/root", runner=runner) == ""


def test_unknown_extension_returns_empty_without_calling_runner():
    def runner(cmd, cwd, timeout_sec):
        raise AssertionError("runner must not be called for an unsupported extension")

    assert diagnose_file("README.md", "/root", runner=runner) == ""


# --- output cap ----------------------------------------------------------------

def test_cap_truncates_100_errors_with_marker():
    def runner(cmd, cwd, timeout_sec):
        return _FakeProc(stdout=_pyright_json(errors=100, warnings=0))

    out = diagnose_file("x.py", "/root", runner=runner)
    assert len(out) <= MAX_DIAGNOSTICS_CHARS
    assert "more)" in out
