"""058 follow-up — `polyrob update` must reinstall with the extras this install
HAS, under the lock. Before this, `pip install -e .` dropped every extra a user
(or core/lazy_deps on first use) had installed: an existing package survives
(pip does not uninstall), but a NEW dependency of that extra never arrives, and
an unconstrained install could move a pinned core package the deploy verified."""
import json
from pathlib import Path

from cli.update import extras as ex

_PYPROJECT = '''
[project]
name = "polyrob"
dependencies = ["openai>=1", "click>=8"]
[project.optional-dependencies]
gemini = ["google-generativeai>=0.8.0"]
docs = ["pypdf>=6", "python-docx>=0.8"]
memory-vector = ["sentence-transformers>=3", "apsw>=3.46", "sqlite-vec>=0.1", "numpy>=1.24"]
dev = ["pytest>=7"]
all = ["polyrob[gemini,docs,memory-vector]"]
'''


def test_declared_extras_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    d = ex.declared_extras(tmp_path)
    assert d["gemini"] == {"google-generativeai"}
    assert d["docs"] == {"pypdf", "python-docx"}
    assert "dev" not in d and "all" not in d, "meta / tooling extras are never reinstalled"


def test_declared_extras_from_dist_metadata_when_no_pyproject(tmp_path, monkeypatch):
    """A wheel install has no pyproject.toml; the dist's Requires-Dist markers say it."""
    class _Dist:
        requires = ['fastapi==0.1; extra == "server"', 'python-magic>=0.4; extra == "server"',
                    'anthropic>=0.20; extra == "anthropic"', 'openai>=1']
        metadata = {"Name": "polyrob"}
    monkeypatch.setattr(ex, "_dist", lambda: _Dist())
    d = ex.declared_extras(tmp_path)
    assert d == {"server": {"fastapi", "python-magic"}, "anthropic": {"anthropic"}}


def test_installed_extras_are_those_whose_every_dist_is_present(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT)
    present = {"google-generativeai", "pypdf", "apsw", "sqlite-vec", "numpy"}
    monkeypatch.setattr(ex, "_dist_present", lambda n: n in present)
    got = ex.installed_extras(tmp_path)
    assert got == ["gemini"]                      # docs lacks python-docx; memory-vector lacks the embedder
    present.add("python-docx")
    assert ex.installed_extras(tmp_path) == ["docs", "gemini"]


def test_pip_target_carries_extras_and_lock(tmp_path):
    (tmp_path / "requirements.lock").write_text("openai==1.0\n")
    assert ex.pip_target(tmp_path, ["gemini", "docs"], editable=True) == \
        ["-c", str(tmp_path / "requirements.lock"), "-e", ".[docs,gemini]"]
    assert ex.pip_target(tmp_path, [], editable=False) == \
        ["-c", str(tmp_path / "requirements.lock"), "."]


def test_pip_target_without_a_lock_is_unconstrained_but_says_so(tmp_path):
    args = ex.pip_target(tmp_path, ["gemini"], editable=True)
    assert "-c" not in args and args[-1] == ".[gemini]"


def test_manual_upgrade_spec_names_the_extras():
    assert ex.upgrade_spec([]) == "polyrob"
    assert ex.upgrade_spec(["server", "docs"]) == '"polyrob[docs,server]"'


def test_install_commands_are_hash_checked_in_two_pip_steps(tmp_path):
    """066 P1 / D2: pip's hash mode cannot hash the project, so the closure goes
    first (--require-hashes) and the project follows --no-deps."""
    (tmp_path / "requirements.lock").write_text("openai==1.0\n")
    deps = tmp_path / "deps.txt"
    cmds = ex.install_commands("/py", tmp_path, ["gemini", "docs"], editable=True, deps_file=deps)
    assert cmds[0][1] == str(tmp_path / "core" / "lock_closure.py")
    assert cmds[0][cmds[0].index("--extras") + 1] == "docs,gemini"
    assert cmds[1] == ["/py", "-m", "pip", "install", "--require-hashes", "--prefer-binary", "-r", str(deps)]
    assert cmds[2] == ["/py", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."]
    assert all("-c" not in c for c in cmds)


def test_install_commands_without_a_lock_fall_back_to_one_pip(tmp_path):
    cmds = ex.install_commands("/py", tmp_path, ["gemini"], editable=False, deps_file=tmp_path / "d")
    assert len(cmds) == 1 and cmds[0][:4] == ["/py", "-m", "pip", "install"]
