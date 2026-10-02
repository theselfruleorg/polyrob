"""install.sh --packs (067, one install): the first-party packs ship inside
polyrob, so --packs installs each pack's SDK extra, hash-checked from THIS tree.

Runs the real installer against a stub source tree (the real lock, pyproject,
core/lock_closure.py and packs/*/*/pack.toml) with a python stub whose pip
calls are recorded and succeed — nothing touches the network.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "packs").mkdir(parents=True)
    for rel in ("requirements.lock", "pyproject.toml", "core/lock_closure.py",
                "core/packs/retire.py", "core/packs/index.py"):
        shutil.copy(REPO / rel, src / rel)
    for pp in (REPO / "packs").glob("*/*/pack.toml"):
        dest = src / pp.relative_to(REPO)
        dest.parent.mkdir(parents=True)
        shutil.copy(pp, dest)
    return src


def _stub(tmp_path):
    venv = tmp_path / "venv"
    py = venv / "bin" / "python"
    py.parent.mkdir(parents=True)
    calls = tmp_path / "calls"
    # pip: record argv, copy any -r file aside, succeed. cli.polyrob: succeed.
    py.write_text(
        '#!/bin/bash\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        '  printf "%s\\n" "$*" >> "$CALLS"; printf "%s\\n" "$*" >> "$CALLS.all"\n'
        '  prev=""; for a in "$@"; do [ "$prev" = "-r" ] && cat "$a" >> "$CALLS.reqs"; prev="$a"; done\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "-m" ]; then exit 0; fi\n'
        'case "$1" in *retire.py) printf "RETIRE %s\\n" "$1" >> "$CALLS.all"; exit 0;; esac\n'
        'exec "$REAL_PY" "$@"\n')
    py.chmod(0o755)
    return venv, calls


def _run(tmp_path, *args):
    src = _tree(tmp_path)
    venv, calls = _stub(tmp_path)
    env = {**os.environ, "HOME": str(tmp_path / "home"), "CALLS": str(calls),
           "REAL_PY": sys.executable}
    res = subprocess.run(
        ["bash", str(REPO / "install.sh"), "--dir", str(src), "--home", str(tmp_path / "data"),
         "--venv-dir", str(venv), "--no-setup", "--no-browser", *args],
        env=env, capture_output=True, text=True, timeout=120)
    lines = calls.read_text().splitlines() if calls.exists() else []
    reqs = Path(f"{calls}.reqs").read_text() if Path(f"{calls}.reqs").exists() else ""
    return res, src, lines, reqs


def test_packs_flag_installs_the_pack_extra_hashed_and_no_second_dist(tmp_path):
    res, src, calls, reqs = _run(tmp_path, "--no-prompt", "--packs", "x")
    assert res.returncode == 0, res.stderr
    editable = [c for c in calls if "--no-deps" in c]
    assert len(editable) == 1 and editable[0].endswith(f"-e {src}"), \
        "only core installs --no-deps: the pack ships inside it"
    hashed = [c for c in calls if "--require-hashes" in c]
    assert len(hashed) == 2, "core closure, then the pack's extra closure"
    assert "tweepy==" in reqs and "chatxdk==" in reqs
    assert "anysite-cli==" not in reqs
    assert not any("/packs/" in c for c in calls)


def test_piped_or_no_prompt_default_installs_no_pack_sdk(tmp_path):
    res, src, calls, reqs = _run(tmp_path, "--no-prompt")
    assert res.returncode == 0, res.stderr
    assert not any("/packs/" in c for c in calls)
    assert "tweepy==" not in reqs


def test_all_installs_every_pack_extra(tmp_path):
    res, src, calls, reqs = _run(tmp_path, "--no-prompt", "--packs", "all")
    assert res.returncode == 0, res.stderr
    for pin in ("tweepy==", "anysite-cli==", "hyperliquid-python-sdk==", "py-clob-client-v2=="):
        assert pin in reqs, pin
    assert not any("/packs/" in c for c in calls)


def test_an_unknown_pack_is_refused_by_name(tmp_path):
    res, _, calls, _ = _run(tmp_path, "--no-prompt", "--packs", "nope")
    assert res.returncode != 0
    assert "unknown pack" in res.stderr and "nope" in res.stderr
    assert not any("/packs/" in c for c in calls)


def test_the_flag_is_documented():
    text = (REPO / "install.sh").read_text()
    assert "--packs LIST" in text
    assert text.index("# 4b. First-party pack SDKs") > text.index("# 4. Browser engine")


def test_a_reused_venv_retires_the_old_pack_dists_after_the_project_install(tmp_path):
    """Codex in-wheel review P2: re-running the installer into an existing venv
    must retire the separate pack dists (core/packs/retire.py, a shipped path,
    metadata only) AFTER the project install."""
    res, src, _, _ = _run(tmp_path, "--no-prompt")
    assert res.returncode == 0, res.stderr
    calls = (tmp_path / "calls.all").read_text().splitlines()
    retire = next(i for i, c in enumerate(calls) if c.startswith("RETIRE "))
    project = next(i for i, c in enumerate(calls) if c.endswith(f"-e {src}"))
    assert calls[retire] == f"RETIRE {src}/core/packs/retire.py"
    assert project < retire, "metadata-only retirement AFTER the project install"
