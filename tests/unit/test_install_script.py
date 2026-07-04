"""Smoke tests for install.sh — purely static; never executes the installer."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _script_text() -> str:
    return INSTALL_SH.read_text()


# ---------------------------------------------------------------------------
# Existence + permissions
# ---------------------------------------------------------------------------

def test_install_sh_exists():
    assert INSTALL_SH.is_file(), f"install.sh not found at {INSTALL_SH}"


def test_install_sh_is_executable():
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh is not user-executable"


# ---------------------------------------------------------------------------
# Syntax check (bash -n)
# ---------------------------------------------------------------------------

def test_bash_syntax_parse():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax errors:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Optional: shellcheck (skipped gracefully when not installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    shutil.which("shellcheck") is None,
    reason="shellcheck not installed — skipping (bash -n is the primary parse check)",
)
def test_shellcheck():
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(INSTALL_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shellcheck found warnings/errors:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Key-step presence (guards against accidental removal)
# ---------------------------------------------------------------------------

def test_has_strict_mode():
    assert "set -euo pipefail" in _script_text(), (
        "install.sh must contain 'set -euo pipefail'"
    )


def test_has_shebang():
    first_line = _script_text().splitlines()[0]
    assert first_line.startswith("#!/usr/bin/env bash"), (
        f"Expected '#!/usr/bin/env bash' shebang, got: {first_line!r}"
    )


def test_has_python_version_check():
    text = _script_text()
    # Script must check for python 3.11+ — look for a version comparison or candidate list
    assert "3.11" in text, (
        "install.sh must reference Python 3.11 (minimum version check)"
    )
    # Must also abort with a non-zero exit when python is missing / too old
    assert "die" in text or "exit 1" in text, (
        "install.sh must exit non-zero when Python requirement is not met"
    )


def test_has_venv_creation():
    text = _script_text()
    assert "-m venv" in text, (
        "install.sh must create a virtual environment with 'python -m venv'"
    )


def test_has_install_command():
    text = _script_text()
    # Must install via pip install -e . (editable from pyproject.toml)
    assert "pip install" in text and "-e" in text, (
        "install.sh must contain 'pip install -e .' to install the project"
    )


def test_has_polyrob_init():
    text = _script_text()
    assert "polyrob init" in text, (
        "install.sh must call 'polyrob init' as the final setup step"
    )


def test_init_is_non_interactive():
    text = _script_text()
    # The init call must use --no-prompt or --non-interactive so the installer doesn't hang
    assert "--no-prompt" in text or "--non-interactive" in text, (
        "install.sh must pass --no-prompt (or --non-interactive) to 'polyrob init' "
        "so the script does not block waiting for keyboard input"
    )


def test_uses_polyrob_naming():
    """install.sh must use 'polyrob' (not bare 'rob') in CLI-invocation strings."""
    text = _script_text()
    # Must mention polyrob
    assert "polyrob" in text.lower()
    # The script must NOT invoke the old 'rob' CLI binary (e.g. 'rob run', 'rob init').
    # Allowed: 'polyrob', '~/.rob/' (config dir path), '.rob/' (project dir), 'rob.egg'.
    import re
    # Match standalone 'rob' that is NOT preceded by 'poly' or '.' or '~/'
    # This excludes: polyrob, ~/.rob, .rob/
    standalone_rob = re.findall(r'(?<![./~\w])rob(?!\w|\.egg)', text)
    assert not standalone_rob, (
        f"install.sh should not invoke the old 'rob' CLI binary. "
        f"Found standalone 'rob' occurrences: {standalone_rob}"
    )


def test_no_hardcoded_secrets():
    """Basic guard — script must not contain any common secret patterns."""
    import re
    text = _script_text()
    # Look for things that look like API keys
    secret_pattern = re.compile(
        r'(?:sk-[A-Za-z0-9]{20,}|ANTHROPIC_API_KEY=[A-Za-z0-9]+|OPENAI_API_KEY=[A-Za-z0-9]+)'
    )
    matches = secret_pattern.findall(text)
    assert not matches, f"install.sh appears to contain hardcoded secrets: {matches}"
