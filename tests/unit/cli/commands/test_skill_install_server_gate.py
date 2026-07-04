"""Task 23: skill install is owner/CLI-only on servers (hard gate, no REST route).

``install_local`` is the single seam ``install_git``/``install_url`` funnel
through, so one ``_require_local_operator()`` guard at its top covers all
three install routes. Confirms the gate fires BEFORE any network/git work,
and that there is genuinely no REST endpoint that could bypass it.
"""
import re
import subprocess
from pathlib import Path

import pytest

from cli.commands import skill_install
from cli.commands.skill_install import InstallError, install_local, _approve


def _mkskill(tmp_path, name, desc="Do a thing. Use when needed.", body="# b\ncontent"):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}")
    return d


def _server_mode(monkeypatch):
    from agents.task import constants

    monkeypatch.setattr(constants, "local_mode_enabled", lambda: False)


def test_install_refused_on_server(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _server_mode(monkeypatch)
    d = _mkskill(tmp_path, "s")
    with pytest.raises(Exception) as ei:
        install_local(d, user_id="7", trust="local")
    assert "server" in str(ei.value).lower() or "owner" in str(ei.value).lower()


def test_install_local_allowed_when_local_mode_on(tmp_path, monkeypatch):
    """Sanity check: the gate is a real conditional, not always-raise — with
    local mode ON (the existing pipeline suites' fixture) install still works."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    from agents.task import constants

    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)
    d = _mkskill(tmp_path, "s2")
    res = install_local(d, user_id="7", trust="local")
    assert res.approved is True


def test_install_git_refused_on_server(tmp_path, monkeypatch):
    """install_git funnels through install_local at the end (after cloning) —
    the server refusal must still fire, not silently succeed."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))

    work = tmp_path / "work"
    skill = work / "myskill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: A git skill. Use when git.\n---\n# b\nx"
    )
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)

    _server_mode(monkeypatch)
    with pytest.raises(Exception) as ei:
        skill_install.install_git(f"file://{bare}/myskill", user_id="7", trust="local")
    assert "server" in str(ei.value).lower() or "owner" in str(ei.value).lower()


def test_approve_refused_on_server(tmp_path, monkeypatch):
    """``_approve`` promotes an already-``.pending`` skill to ACTIVE — it must be
    gated too, not just ``install_local``. Otherwise a server could activate a
    quarantined skill with no local-operator check."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    _server_mode(monkeypatch)
    with pytest.raises(Exception) as ei:
        _approve("someskill", user_id="7", source="local")
    assert "server" in str(ei.value).lower() or "owner" in str(ei.value).lower()


def test_install_url_refused_on_server(tmp_path, monkeypatch):
    """install_url funnels through install_local at the end (after fetching) —
    the server refusal must still fire, not silently succeed."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(
        skill_install,
        "_fetch_text",
        lambda url, **k: "---\nname: fetched\ndescription: A fetched skill. Use it.\n---\n# b\nx",
    )
    _server_mode(monkeypatch)
    with pytest.raises(Exception) as ei:
        skill_install.install_url("https://example.com/fetched/SKILL.md", user_id="7", trust="local")
    assert "server" in str(ei.value).lower() or "owner" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Repo invariant: no REST route registers a skill-install handler.
# ---------------------------------------------------------------------------


def test_no_rest_skill_install_endpoint_exists():
    """There must be NO REST/API route that can install a skill — the ONLY
    install surface is the CLI/local pipeline gated by ``_require_local_operator``
    above. Grep every router module under ``api/`` for a route decorator whose
    path or handler name mentions "install" in a skill context. Skipped
    gracefully if the ``api/`` package layout ever changes (this is a
    best-effort invariant check, not a hard dependency on today's file names)."""
    repo_root = Path(__file__).resolve().parents[4]
    api_dir = repo_root / "api"
    if not api_dir.is_dir():
        pytest.skip("api/ directory not found at expected location")

    route_decorator_re = re.compile(
        r'@\w+\.(?:get|post|put|patch|delete)\(\s*["\'][^"\']*install[^"\']*["\']', re.IGNORECASE
    )
    handler_def_re = re.compile(
        r"^\s*(?:async\s+)?def\s+\w*install\w*\s*\(", re.IGNORECASE | re.MULTILINE
    )

    offenders = []
    for py_file in api_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        if "skill" not in text.lower():
            continue
        if route_decorator_re.search(text) or handler_def_re.search(text):
            offenders.append(str(py_file.relative_to(repo_root)))

    assert not offenders, (
        f"found a possible REST skill-install route/handler in {offenders} — "
        "skill install must remain owner/CLI-only with NO REST endpoint"
    )
