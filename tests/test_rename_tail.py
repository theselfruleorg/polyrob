import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _grep(pattern, *paths):
    return subprocess.run(["git", "grep", "-nP", pattern, "--", *paths],
                          cwd=ROOT, capture_output=True, text=True).stdout

def test_no_rob_service_in_user_docs():
    # user-facing service references must be the polyrob unit name; the negative
    # lookbehind excludes the CORRECT `polyrob.service` (which contains the
    # substring `rob.service`) so only a stray OLD `rob.service` fails.
    assert _grep(r"(?<!poly)rob\.service", "AGENTS.md", "webview/server.py") == ""
