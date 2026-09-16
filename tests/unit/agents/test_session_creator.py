"""043 A17 — the session `creator` label.

`creator` is a DISPLAY SSOT for "who/what started this session", persisted
ONCE at genuine creation (``SessionManager.create_session``). It is
independent of — and reconciled with, never replacing — the two existing
markers: ``session_source`` (a routing fact: which chat surface/key a
session is bound to) and ``mark_autonomous`` (an in-process autonomy-registry
flag the goal tool's self-mutation guard consumes). All three coexist.

``resolve_creator`` (the resolution TaskAgent.create_session drives) is a
PURE function — no container, no SessionManager instance needed to test it.
"""
from agents.task.agent.session import SessionManager, resolve_creator
from agents.task.goals.autonomy_marker import is_autonomous, mark_autonomous


def _sm(tmp_path):
    """A SessionManager rooted at tmp_path, with pm() pointed at the same
    root so create_session's on-disk writes and a fresh instance's
    _load_sessions_from_disk() glob agree on where to look."""
    from agents.task.path import get_path_manager, set_path_manager
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    return SessionManager(base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# SessionManager.create_session — creator persists across a reload from disk
# ---------------------------------------------------------------------------

def test_cron_created_session_persists_and_reloads_creator(tmp_path):
    """A cron-created session carries creator == 'cron' — including across a
    BRAND-NEW SessionManager instance re-reading it from disk (the real
    persistence path: _save_metadata -> metadata.json -> _load_sessions_from_disk)."""
    sm1 = _sm(tmp_path)
    sid = sm1.create_session("cron-sess", user_id="u1", creator="cron")
    assert sm1.get_session_info(sid)["creator"] == "cron"

    sm2 = SessionManager(base_dir=str(tmp_path))
    reloaded = sm2.get_session_info(sid)
    assert reloaded is not None
    assert reloaded["creator"] == "cron"


def test_create_session_default_creator_is_api(tmp_path):
    """No creator= passed -> the honest HTTP/A2A default, never a missing key."""
    sm = _sm(tmp_path)
    sid = sm.create_session("plain-sess", user_id="u1")
    assert sm.get_session_info(sid)["creator"] == "api"


def test_create_session_creator_set_once_not_overwritten_on_resume(tmp_path):
    """creator is set ONCE, at genuine creation — a repeat create_session call
    for an id that already exists (the resume path) must not re-stamp it."""
    sm = _sm(tmp_path)
    sid = sm.create_session("resume-sess", user_id="u1", creator="owner")
    again = sm.create_session(sid, user_id="u1", creator="cron")
    assert again == sid
    assert sm.get_session_info(sid)["creator"] == "owner"


def test_creator_and_mark_autonomous_coexist(tmp_path):
    """043 A17 reconciliation: `creator` (display SSOT) and `mark_autonomous`
    (autonomy-registry membership) are independent inputs that both survive —
    neither is removed nor replaced by the other."""
    sm = _sm(tmp_path)
    sid = sm.create_session("goal-sess", user_id="u1", creator="goal")
    mark_autonomous(sid, goal_id="g1")
    try:
        assert sm.get_session_info(sid)["creator"] == "goal"
        assert is_autonomous(sid) is True
    finally:
        from agents.task.goals import autonomy_marker
        autonomy_marker._SESSIONS.pop(sid, None)


# ---------------------------------------------------------------------------
# resolve_creator — the pure resolver TaskAgent.create_session drives.
# Needs no container/SessionManager instance at all.
# ---------------------------------------------------------------------------

class _FakeSessionSource:
    def __init__(self, surface_id, tier=None):
        self.surface_id = surface_id
        if tier is not None:
            self.tier = tier


def test_resolve_creator_explicit_kwarg_always_wins():
    assert resolve_creator("owner", _FakeSessionSource("api")) == "owner"
    assert resolve_creator("goal", None) == "goal"


def test_resolve_creator_telegram_session_source_is_owner():
    from core.surfaces.envelopes import SessionSource
    src = SessionSource(surface_id="telegram", chat_id="123")
    assert resolve_creator(None, src) == "owner"


def test_resolve_creator_every_known_chat_surface_is_owner():
    from core.surfaces.envelopes import SessionSource
    # `email` is deliberately absent — see tests/unit/agents/test_resolve_creator_email.py
    for surface_id in ("telegram", "whatsapp", "discord", "slack", "signal",
                        "x", "webview"):
        src = SessionSource(surface_id=surface_id, chat_id="c")
        assert resolve_creator(None, src) == "owner", surface_id


def test_resolve_creator_cli_sources_are_cli():
    from core.surfaces.envelopes import SessionSource
    for surface_id in ("cli", "repl", "local"):
        src = SessionSource(surface_id=surface_id, chat_id="c")
        assert resolve_creator(None, src) == "cli", surface_id


def test_resolve_creator_nothing_is_api():
    assert resolve_creator(None, None) == "api"
    assert resolve_creator("", None) == "api"


def test_resolve_creator_unknown_surface_is_api():
    from core.surfaces.envelopes import SessionSource
    src = SessionSource(surface_id="some-future-surface", chat_id="c")
    assert resolve_creator(None, src) == "api"


def test_resolve_creator_correspondent_tier_hook_overrides_owner_surface():
    """Forward-compat: if session_source ever carries a tier/kind naming
    correspondent, resolve_creator honors it over the surface_id mapping —
    even for a surface_id ("telegram") that would otherwise resolve owner."""
    src = _FakeSessionSource("telegram", tier="correspondent")
    assert resolve_creator(None, src) == "correspondent"
