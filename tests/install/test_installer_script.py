"""install.sh — the one-liner contract (062).

The regression this file exists for: `curl … | bash` died on line 1 with
`BASH_SOURCE[0]: unbound variable` (set -u, and BASH_SOURCE is unset when the
script arrives on stdin), and then refused because it never cloned. Both are
only reachable by actually PIPING the script, so every test here does.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _pipe(args=(), env=None, input_text=None):
    """Run the installer the way the website tells people to: through a pipe."""
    script = INSTALLER.read_text()
    cmd = ["bash", "-s", "--", *args]
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(cmd, input=input_text if input_text is not None else script,
                          capture_output=True, text=True, env=full_env, timeout=120)


def test_installer_parses():
    res = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_piped_help_does_not_crash_on_unbound_bash_source():
    """THE regression. A piped run must never reference BASH_SOURCE unguarded."""
    res = _pipe(["--help"])
    assert res.returncode == 0, res.stderr
    assert "unbound variable" not in res.stderr
    assert "--uninstall" in res.stdout


def test_piped_unknown_flag_fails_loudly():
    res = _pipe(["--definitely-not-a-flag"])
    assert res.returncode != 0
    assert "Unknown argument" in res.stderr


def test_bash_source_is_guarded_in_source():
    src = INSTALLER.read_text()
    assert "${BASH_SOURCE[0]:-}" in src, (
        "a bare ${BASH_SOURCE[0]} under `set -u` is the piped-install crash")


def test_the_shim_clears_python_env_and_runs_the_module():
    """The launcher must not inherit PYTHONPATH (it would import another
    checkout) and must run `-m cli.polyrob`, never a console script that a
    stale wheel on PATH could shadow."""
    src = INSTALLER.read_text()
    assert "unset PYTHONPATH" in src
    assert "unset PYTHONHOME" in src
    assert "-m cli.polyrob" in src
    assert 'rm -f "${SHIM}"' in src, (
        "cat > over an old SYMLINK follows it into the venv — remove first")


def test_path_block_markers_match_the_python_uninstaller():
    """install.sh writes the block; `polyrob uninstall` strips it. One pair of
    markers, or one of them silently leaves the other's lines behind."""
    from cli.commands.uninstall import _BEGIN, _END

    src = INSTALLER.read_text()
    assert _BEGIN in src and _END in src


def test_uninstall_keeps_the_data_home(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    shim = home / ".local" / "bin" / "polyrob"
    shim.write_text("#!/usr/bin/env bash\nexec true\n")
    shim.chmod(0o755)
    data = tmp_path / "polyrob-home"
    (data / "wallet").mkdir(parents=True)
    (data / "wallet" / "meta.json").write_text("{}")
    rc = home / ".bashrc"
    rc.write_text('export FOO=1\n# >>> polyrob >>>\nexport PATH="x:$PATH"\n# <<< polyrob <<<\nexport BAR=2\n')

    res = _pipe(["--uninstall", "--home", str(data)],
                env={"HOME": str(home), "SHELL": "/bin/bash"})
    assert res.returncode == 0, res.stderr + res.stdout
    assert not shim.exists()
    assert (data / "wallet" / "meta.json").is_file(), "data must survive an uninstall"
    body = rc.read_text()
    assert "polyrob" not in body
    assert "export FOO=1" in body and "export BAR=2" in body


def test_source_mode_refuses_a_directory_without_pyproject(tmp_path):
    res = _pipe(["--dir", str(tmp_path), "--no-prompt"])
    assert res.returncode != 0
    assert "pyproject.toml not found" in res.stderr


def test_browser_is_opt_in_not_a_lazy_dep():
    """A ~150 MB Chromium download must never happen inside a turn; the
    installer is where it is offered, and core/lazy_deps must not carry it."""
    from core.lazy_deps import LAZY_DEPS

    assert not any("playwright" in spec for specs in LAZY_DEPS.values() for spec in specs)
    assert "--no-browser" in INSTALLER.read_text()


def test_the_installer_never_calls_a_verb_it_has_not_checked_for():
    """⚠️ The installer is served from the site and clones the PUBLIC branch, so
    it can be newer than the code it installs. It called `polyrob setup` — a
    verb 1.1.0 does not have — and an interactive install died on it. Every
    version-dependent verb now goes through has_verb()/wizard_verb()."""
    src = INSTALLER.read_text()
    assert "has_verb()" in src and "wizard_verb()" in src
    # The wizard is invoked through the resolved name, never a literal.
    assert '-m cli.polyrob "${WIZARD}"' in src
    assert '-m cli.polyrob setup' not in src
    # `init` is the one verb every version has, so the non-interactive
    # fallback may name it directly.
    assert "-m cli.polyrob init --no-prompt" in src


def test_the_banner_gates_the_new_verbs_on_their_presence():
    src = INSTALLER.read_text()
    assert "has_verb service" in src
    assert "has_verb uninstall" in src


def _root_trust(path):
    """Run install.sh's root-trust helper on *path*; return what it prints."""
    src = INSTALLER.read_text()
    block = src.split("# >>> root-trust >>>", 1)[1].split("# <<< root-trust <<<", 1)[0]
    script = f'PYTHON_BIN=python3\n{block}\nuntrusted_root_path "$1"\n'
    res = subprocess.run(["bash", "-c", script, "bash", str(path)],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_root_install_refuses_a_user_owned_tree(tmp_path):
    """OPS-15: a root install writes a global shim; the tree it runs must be root's."""
    if os.geteuid() == 0:
        pytest.skip("the tmp tree is root-owned when the suite runs as root")
    assert _root_trust(tmp_path / "src" / ".venv") != ""


def test_root_install_accepts_a_root_owned_tree():
    st = os.stat("/")
    if st.st_uid != 0 or st.st_mode & 0o022:
        pytest.skip("this host's / is not a root-owned, non-writable directory")
    assert _root_trust("/") == ""


def test_root_install_check_runs_before_the_venv_is_built():
    src = INSTALLER.read_text()
    assert src.index('untrusted_root_path "${_tree}"') < src.index("# 3. Virtualenv + install")
