"""`_load_skill_content(strict=True)` raises where the default form logs and
returns "": an unreadable file, unreadable revision metadata, a body that does
not match its recorded revision. The console skill route reads strict, so an
unreadable skill is an error, never a 404 "missing"."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.task.agent.skill_manager import SkillManager

BODY = "---\nname: demo-skill\ndescription: d\n---\n# Demo\n\nBody.\n"


@pytest.fixture(autouse=True)
def _no_pack_skills(monkeypatch):
    from core.skills import scopes
    monkeypatch.setattr(scopes, "pack_scopes", lambda: [])


@pytest.fixture
def sm(tmp_path):
    d = tmp_path / "skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(BODY)
    return SkillManager(skills_dir=tmp_path / "skills")


def test_an_unreadable_file_raises_only_when_strict(sm, monkeypatch):
    real = Path.read_text

    def _refuse(self, *a, **kw):
        if self.name == "SKILL.md":
            raise PermissionError("denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _refuse)
    assert sm._load_skill_content("demo-skill") == ""
    with pytest.raises(PermissionError):
        sm._load_skill_content("demo-skill", strict=True)


def _as_user_skill(sm, tmp_path, monkeypatch, expected):
    res = SimpleNamespace(scope="user", external=None, path=tmp_path / "skills" / "demo-skill")
    monkeypatch.setattr(sm, "_resolve", lambda sid, uid=None: res)
    monkeypatch.setattr(sm, "_require_user", lambda uid: uid)
    monkeypatch.setattr(sm, "_read_skill_text", lambda p: BODY)
    monkeypatch.setattr(sm, "_expected_content_hash", expected)


def test_unreadable_revision_metadata_raises_only_when_strict(sm, tmp_path, monkeypatch):
    def _boom(uid, sid):
        raise OSError("revision store unreadable")
    _as_user_skill(sm, tmp_path, monkeypatch, _boom)
    assert sm._load_skill_content("demo-skill", user_id="alice") == ""
    with pytest.raises(Exception, match="revision"):
        sm._load_skill_content("demo-skill", user_id="alice", strict=True)


def test_a_hash_mismatch_raises_only_when_strict(sm, tmp_path, monkeypatch):
    _as_user_skill(sm, tmp_path, monkeypatch, lambda uid, sid: "0" * 64)
    assert sm._load_skill_content("demo-skill", user_id="alice") == ""
    with pytest.raises(Exception, match="revision"):
        sm._load_skill_content("demo-skill", user_id="alice", strict=True)


def test_a_matching_user_skill_loads_strict(sm, tmp_path, monkeypatch):
    good = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    _as_user_skill(sm, tmp_path, monkeypatch, lambda uid, sid: good)
    assert "# Demo" in sm._load_skill_content("demo-skill", user_id="alice", strict=True)


def test_the_console_route_answers_an_unreadable_skill_as_an_error(monkeypatch, tmp_path):
    import agents.task.agent.skill_manager as skm
    import webview.knowledge as kn

    class _SM:
        def _load_skill_content(self, skill_id, user_id=None, strict=False):
            if strict:
                raise PermissionError("denied")
            return ""

    monkeypatch.setattr(skm, "get_skill_manager", lambda: _SM())
    monkeypatch.setattr(kn, "_effective_user_id", lambda req: "alice")
    app = FastAPI()
    app.include_router(kn.router)
    resp = TestClient(app).get("/api/webgate/knowledge/skill/demo-skill")
    assert resp.status_code != 404, resp.text
    assert resp.json()["body"] is None and resp.json()["error"]
