"""/learn — distills a described procedure into a quarantined skill."""
from types import SimpleNamespace

from cli.ui.commands import h_learn


class _FakeSM:
    def __init__(self, ok=True, pending=True):
        self.created = {}
        self._ok = ok
        self._pending = pending

    def validate_skill_id(self, sid):
        return (True, [])

    def create_skill(self, sid, content, *, user_id, description="", created_by="agent",
                     pending=None):
        self.created = {"sid": sid, "content": content, "user_id": user_id,
                        "created_by": created_by, "pending": pending}
        return SimpleNamespace(ok=self._ok, pending=self._pending, errors=[],
                               skill_id=sid, path="/p")


def _ctx(args, sm, user_id="u1"):
    emitted = {}
    ctx = SimpleNamespace(
        args=args, user_id=user_id,
        emit=lambda text, title=None: emitted.update({"text": text, "title": title}))
    return ctx, emitted


def test_slug_derivation():
    assert h_learn._slug("When deploying always run tests first") == "deploying-run-tests-first"
    assert h_learn._slug("").startswith("learned")
    # leading non-alpha is fixed up
    assert h_learn._slug("123 go")[0].isalpha()


def test_learn_creates_pending_skill(monkeypatch):
    sm = _FakeSM(ok=True, pending=True)
    monkeypatch.setattr(h_learn, "_skill_manager", lambda ctx: sm)
    ctx, emitted = _ctx(["When", "deploying", "always", "run", "tests"], sm)
    h_learn.h_learn(ctx)
    assert sm.created["sid"]
    assert sm.created["created_by"] == "agent"
    assert sm.created["pending"] is True  # quarantine FORCED, not flag-dependent
    assert "deploy" in sm.created["content"].lower()
    assert "pending" in emitted["text"].lower()
    assert "/pending promote skill" in emitted["text"]


def test_learn_empty_description_shows_usage(monkeypatch):
    sm = _FakeSM()
    monkeypatch.setattr(h_learn, "_skill_manager", lambda ctx: sm)
    ctx, emitted = _ctx([], sm)
    h_learn.h_learn(ctx)
    assert "usage:" in emitted["text"]
    assert sm.created == {}  # nothing written
    assert emitted["title"] == "learn"  # title on every emit (Wave B2)


def test_learn_rejected_surfaces_error(monkeypatch):
    class _RejectSM(_FakeSM):
        def create_skill(self, *a, **k):
            return SimpleNamespace(ok=False, pending=False,
                                   errors=["failed identity scan"], skill_id="x", path="")
    sm = _RejectSM()
    monkeypatch.setattr(h_learn, "_skill_manager", lambda ctx: sm)
    ctx, emitted = _ctx(["do", "a", "bad", "thing"], sm)
    h_learn.h_learn(ctx)
    assert "rejected" in emitted["text"].lower()
    assert emitted["title"] == "learn"


def test_learn_unavailable_no_skill_manager_has_title(monkeypatch):
    monkeypatch.setattr(h_learn, "_skill_manager", lambda ctx: None)
    ctx, emitted = _ctx(["do", "a", "thing"], None)
    h_learn.h_learn(ctx)
    assert "unavailable" in emitted["text"]
    assert emitted["title"] == "learn"


def test_distill_produces_valid_structure():
    sid, content = h_learn._distill("Rotate the API key monthly and notify the team.")
    assert sid and sid[0].isalpha()
    assert content.startswith("# ")
    assert "## When to use" in content and "## Procedure" in content
