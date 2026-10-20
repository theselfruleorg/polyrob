"""A sub-agent's virtual session id must pass the path manager's session-id rule.

2026-10-14 16:12Z prod: the owner-chat session (a 36-char UUID) delegated, the
child's id ``<uuid>__sub-1-09e3c2be`` was 52 chars, and every filesystem call in
the child failed "Session ID must contain 1-50 ASCII letters, digits, underscores
or hyphens" (128 errors in two minutes). Before 2026-10-08 the path manager
silently truncated to 50; now it refuses, so the id must be built to fit.
"""
import re

from agents.task.agent.sub_agent_manager import SubAgentManager

UUID = "09e3c2be-7559-4c79-a0fd-81ab13b5a527"
RULE = re.compile(r"[a-zA-Z0-9_-]{1,50}")


def _mgr(session_id):
    mgr = object.__new__(SubAgentManager)
    mgr.session_id = session_id
    mgr._counter = 0
    return mgr


def test_uuid_parent_child_id_fits_the_path_rule():
    mgr = _mgr(UUID)
    for _ in range(12):  # two-digit counters too
        vid = mgr._generate_virtual_session_id(mgr._generate_sub_agent_id("p"))
        assert RULE.fullmatch(vid), vid


def test_children_of_one_parent_stay_distinct():
    mgr = _mgr(UUID)
    ids = {mgr._generate_virtual_session_id(mgr._generate_sub_agent_id("p")) for _ in range(5)}
    assert len(ids) == 5


def test_short_parent_id_is_unchanged():
    mgr = _mgr("abc123")
    vid = mgr._generate_virtual_session_id(mgr._generate_sub_agent_id("p"))
    assert vid == "abc123__sub-1-abc123"


def test_path_manager_accepts_the_child_id():
    from agents.task.path import pm
    mgr = _mgr(UUID)
    vid = mgr._generate_virtual_session_id(mgr._generate_sub_agent_id("p"))
    assert pm().clean_session_id(vid) == vid


def test_uuid_parent_stays_whole_so_run_key_ties_child_to_parent():
    # Release review 1.4.x: the parent was trimmed to 34 chars, so
    # refusal_repeat.run_key(child) != run_key(parent).
    from tools.defi.refusal_repeat import run_key
    mgr = _mgr(UUID)
    for _ in range(12):
        vid = mgr._generate_virtual_session_id(mgr._generate_sub_agent_id("p"))
        assert run_key(vid) == run_key(UUID) == UUID, vid
