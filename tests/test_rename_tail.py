import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _grep(pattern, *paths):
    return subprocess.run(["git", "grep", "-n", pattern, "--", *paths],
                          cwd=ROOT, capture_output=True, text=True).stdout

def test_no_rob_service_in_user_docs():
    # user-facing service references must be the polyrob unit name
    assert _grep(r"rob\.service", "AGENTS.md", "webview/server.py") == ""
