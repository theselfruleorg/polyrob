from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_required_public_files_exist():
    for rel in [
        "CONTRIBUTING.md",
        "SECURITY.md",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
    ]:
        assert (ROOT / rel).is_file(), f"missing {rel}"


def test_ci_runs_gitleaks_and_pytest():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "gitleaks" in ci and "pytest" in ci
