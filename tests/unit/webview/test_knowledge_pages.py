"""C2 — /knowledge read-only wiki endpoints.

Same contract proof as test_webgate_pages.py: each JSON endpoint REUSES the
existing reader (notes verbs / recall_episodes / kb_list_sources / SkillManager /
event log), mocked at the ``webview.knowledge`` (or provider) seam. Fail-open:
no provider -> empty result, never a 500. Tenancy comes from
``webview.pages._effective_user_id`` (fail-CLOSED in multitenant — covered by
test_pages_tenant_scoping.py; not re-proven here).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    import webview.knowledge as knowledge
    app = FastAPI()
    app.include_router(knowledge.router)
    return TestClient(app), knowledge


class _NotesProvider:
    def __init__(self):
        self.calls = {}

    async def note_list(self, user_id, *, status="active", tag=None, limit=200):
        self.calls["note_list"] = dict(user_id=user_id, status=status, tag=tag)
        if status == "active":
            return [{"id": 1, "title": "prod deploys", "content": "x",
                     "tags": ["ops"], "links": ["runbook"], "source": "session:s1",
                     "created_ts": 1752200000, "updated_ts": 1752200000,
                     "access_count": 0, "status": "active", "created_by": "agent"}]
        return []

    async def note_get(self, user_id, note_id, *, bump_access=True):
        self.calls["note_get"] = dict(user_id=user_id, note_id=note_id,
                                      bump_access=bump_access)
        if note_id != 1:
            return None
        return {"id": 1, "title": "prod deploys", "content": "x", "tags": [],
                "links": [], "source": None, "created_ts": 1752200000,
                "updated_ts": 1752200000, "access_count": 1,
                "status": "active", "created_by": "agent"}

    async def note_backlinks(self, user_id, title):
        return []

    async def recall_episodes(self, *, user_id=None, since_ts=None, until_ts=None,
                              kind=None, thread_key=None, limit=20, order="newest",
                              exclude_surfaced=False):
        self.calls["recall_episodes"] = dict(user_id=user_id, since_ts=since_ts,
                                             kind=kind, limit=limit)
        class _E:
            ts = 1752200000; session_id = "s1"; kind = "goal"
            task = "draft tweet"; outcome = "done"; summary = "did it"
            artifacts = [{"path": "report.md"}]; spend_usd = 0.42; steps = 7
            goal_id = "g1"
        return [_E()]

    async def kb_list_sources(self, *, user_id=None, collection=None):
        self.calls["kb_list_sources"] = dict(user_id=user_id, collection=collection)
        return [{"user_id": user_id, "collection": "default",
                 "source_path": "docs/a.md", "source_hash": "h",
                 "chunk_count": 3, "mime": "text/markdown", "created_at": None}]


@pytest.fixture
def seam(monkeypatch):
    client, knowledge = _client()
    prov = _NotesProvider()
    monkeypatch.setattr(knowledge, "_memory_provider", lambda: prov)
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "owner-1")
    return client, knowledge, prov


def test_notes_endpoint_reuses_note_list(seam):
    client, _, prov = seam
    r = client.get("/api/webgate/knowledge/notes")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["title"] == "prod deploys"
    assert body["items"][0]["updated_day"]  # server-side day formatting
    assert prov.calls["note_list"] == dict(user_id="owner-1", status="active", tag=None)


def test_note_detail_404_when_missing(seam):
    client, _, prov = seam
    assert client.get("/api/webgate/knowledge/note/999").status_code == 404
    r = client.get("/api/webgate/knowledge/note/1")
    assert r.status_code == 200
    assert r.json()["note"]["id"] == 1
    # READ-ONLY page: a passive web view must not bump the agent-reuse counter.
    assert prov.calls["note_get"]["bump_access"] is False


def test_episodes_endpoint_reuses_recall(seam):
    client, _, prov = seam
    r = client.get("/api/webgate/knowledge/episodes", params={"since_hours": 8, "kind": "goal"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["outcome"] == "done"
    assert items[0]["artifacts"] == [{"path": "report.md"}]
    assert prov.calls["recall_episodes"]["kind"] == "goal"
    assert prov.calls["recall_episodes"]["since_ts"] is not None


def test_kb_endpoint_reuses_list_sources(seam):
    client, _, prov = seam
    r = client.get("/api/webgate/knowledge/kb")
    assert r.status_code == 200
    assert r.json()["items"][0]["source_path"] == "docs/a.md"
    assert prov.calls["kb_list_sources"]["user_id"] == "owner-1"


def test_no_provider_degrades_to_empty(monkeypatch):
    client, knowledge = _client()
    monkeypatch.setattr(knowledge, "_memory_provider", lambda: None)
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "owner-1")
    for url in ("/api/webgate/knowledge/notes", "/api/webgate/knowledge/episodes",
                "/api/webgate/knowledge/kb"):
        r = client.get(url)
        assert r.status_code == 200
        assert r.json()["items"] == []


def test_changes_endpoint_queries_event_log(seam, monkeypatch):
    client, knowledge, _ = seam
    import core.event_log as ev_mod

    class _Log:
        def query(self, *, since_ts=None, kind=None, user_id=None, limit=500):
            assert user_id == "owner-1"
            if kind == "self_modification":
                return [{"ts": 1752200000, "kind": kind, "source": "memory_tool",
                         "attrs": {"action": "create", "item_id": "1"}}]
            return []

    monkeypatch.setattr(ev_mod, "get_event_log", lambda db_path=None: _Log())
    r = client.get("/api/webgate/knowledge/changes")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["attrs"]["action"] == "create"


def test_skills_endpoint_failopen(monkeypatch):
    """SkillManager unavailable -> empty catalog, never a 500."""
    client, knowledge = _client()
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "owner-1")
    monkeypatch.setattr(knowledge, "_data_dir", lambda: "/nonexistent")
    r = client.get("/api/webgate/knowledge/skills")
    assert r.status_code == 200
    assert "catalog" in r.json()


def test_skills_endpoint_reports_error_field(monkeypatch):
    """030 D4 / 043 A9: a RAISING skill-manager yields the same JSON shape with
    ``error`` set AND ``items``/``catalog``/``count`` null, still HTTP 200 (the
    page renders a distinct error state) — a broken catalog read must not
    masquerade as ``count: 0``, and an empty LIST is exactly that sentence."""
    client, knowledge = _client()
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "owner-1")
    monkeypatch.setattr(knowledge, "_data_dir", lambda: "/nonexistent")
    import agents.task.agent.skill_manager as sm_mod

    def _raise():
        raise RuntimeError("skills dir unreadable")

    monkeypatch.setattr(sm_mod, "get_skill_manager", _raise)
    r = client.get("/api/webgate/knowledge/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] is None and body["catalog"] is None
    assert body["count"] is None
    assert "skills dir unreadable" in body["error"]


def test_skills_endpoint_reports_no_error_on_success(monkeypatch):
    """The happy path carries ``error: None`` — the key is always present (A9),
    and ``items`` is a real empty list, which means "I read it, it is empty"."""
    client, knowledge = _client()
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "owner-1")
    monkeypatch.setattr(knowledge, "_data_dir", lambda: "/nonexistent")
    import agents.task.agent.skill_manager as sm_mod

    class _SM:
        def get_catalog_skills(self, *, user_id=None, max_skills=200):
            return []

        def list_pending_skills(self, user_id):
            return []

    monkeypatch.setattr(sm_mod, "get_skill_manager", lambda: _SM())
    body = client.get("/api/webgate/knowledge/skills").json()
    assert body["error"] is None
    assert body["items"] == [] and body["catalog"] == [] and body["count"] == 0
    # The reuse-stats leg is a SEPARATE read (its store is not on this path in
    # this fixture) and names itself rather than decorating every row with a
    # silent zero — A9's ``usage_error``.
    assert "usage_error" in body


# --- 2026-09-21 revalidation: "not configured" is not "unreadable" ----------- #

@pytest.mark.parametrize("path", ["/api/webgate/knowledge/notes",
                                  "/api/webgate/knowledge/episodes",
                                  "/api/webgate/knowledge/kb"])
def test_no_provider_is_an_empty_answer_with_a_reason_not_an_error(monkeypatch,
                                                                   path):
    """⚠️ A9 residue. With no memory provider these answered ``error:
    "memory_provider_unavailable"`` (and two siblings ``notes_unavailable`` /
    ``episodes_unavailable``), and every client treats a set ``error`` as a
    FAILED READ — `agent.js::renderKb` draws the dashed unreadable entry and
    puts the raw token on screen. An instance with no memory backend was told
    the console could not read its knowledge base, in a machine name."""
    client, knowledge = _client()
    monkeypatch.setattr(knowledge, "_memory_provider", lambda: None)
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "alice")

    body = client.get(path).json()
    assert body["items"] == [] and body["count"] == 0
    assert body["error"] is None, "a configuration fact is not a read failure"
    assert body["reason"], "an empty list with no reason is a confident zero"
    # Owner copy, never a machine token.
    assert "_unavailable" not in body["reason"] and " " in body["reason"]


def test_a_provider_that_raises_is_still_an_error_with_null_items(monkeypatch):
    """The other half of the same rule: a FAULT keeps ``items: null``."""
    client, knowledge = _client()

    class _Boom:
        async def kb_list_sources(self, *, user_id=None, collection=None):
            raise OSError("memory.db is locked")

    monkeypatch.setattr(knowledge, "_memory_provider", lambda: _Boom())
    monkeypatch.setattr(knowledge, "_effective_user_id", lambda request: "alice")

    body = client.get("/api/webgate/knowledge/kb").json()
    assert body["items"] is None and body["count"] is None
    assert "memory.db is locked" in body["error"]
