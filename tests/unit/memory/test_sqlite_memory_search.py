"""UP-09 Step 9.2 — SqliteMemoryProvider.search() tenant isolation, limit, sort, browse.

prefetch() must keep its exact legacy shape (rank-ordered, top_k, "" on no-terms/anon).

T2.6 (2026-07-22) adds: `before_id` rowid-cursor pagination (+ `with_ids` id display,
opt-in — legacy callers that don't ask for it get the byte-identical old format) and
automation-source demotion (rows whose session has a completed 'cron'/'goal' episode
sort after interactive ones, for rank-sorted queries only).
"""
import asyncio
import os
import re

import pytest

from core.sqlite_util import execute_retry
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


def _ids(text: str) -> list:
    """Pull the `(id N)` tags a with_ids=True result embeds, in appearance order."""
    return [int(m) for m in re.findall(r"\(id (\d+)\)", text)]


def _tag_episode(provider, *, session_id: str, user_id: str, kind: str) -> None:
    """Insert a minimal completed episode row (mirrors finalize_episode's shape)
    without going through the EPISODIC_MEMORY_ENABLED-gated write path — this
    directly exercises the RECALL-side demotion query against real episode data."""
    execute_retry(
        provider.db_path,
        "INSERT INTO episodes (ts, user_id, session_id, kind, artifacts, spend_usd, "
        "steps, surfaced, meta, created_at) VALUES (0, ?, ?, ?, '[]', 0, 0, 0, '{}', 0)",
        (user_id, session_id, kind))


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"), top_k=5)


def _seed(provider, user_id, contents, session="s1"):
    for i, c in enumerate(contents):
        asyncio.run(provider.sync_turn(c, f"reply {i}", session_id=session, user_id=user_id))


def test_search_tenant_isolation(provider):
    _seed(provider, "alice", ["alpha widget deployment"])
    _seed(provider, "bob", ["alpha widget deployment"])
    a = asyncio.run(provider.search("widget", user_id="alice"))
    b = asyncio.run(provider.search("widget", user_id="bob"))
    assert "alpha widget" in a
    assert "alpha widget" in b
    # Each only sees its own row (one match each, not two).
    assert a.count("- ") == 1
    assert b.count("- ") == 1


def test_search_respects_limit(provider):
    _seed(provider, "alice", [f"widget number {i}" for i in range(10)])
    res = asyncio.run(provider.search("widget", user_id="alice", limit=2))
    assert res.count("- ") == 2


def test_search_limit_clamped(provider):
    _seed(provider, "alice", [f"widget {i}" for i in range(30)])
    res = asyncio.run(provider.search("widget", user_id="alice", limit=999))
    assert res.count("- ") <= 20
    res0 = asyncio.run(provider.search("widget", user_id="alice", limit=0))
    assert res0.count("- ") == 1  # clamped up to 1


def test_search_sort_newest_oldest(provider):
    _seed(provider, "alice", ["widget first", "widget second", "widget third"])
    newest = asyncio.run(provider.search("widget", user_id="alice", limit=1, sort="newest"))
    oldest = asyncio.run(provider.search("widget", user_id="alice", limit=1, sort="oldest"))
    assert "third" in newest
    assert "first" in oldest


def test_browse_empty_query_recent(provider):
    _seed(provider, "alice", ["widget first", "gadget second", "gizmo third"])
    res = asyncio.run(provider.search("", user_id="alice", limit=2))
    # browse => most-recent rows regardless of keyword
    assert "third" in res
    assert "second" in res
    assert "first" not in res


def test_browse_tenant_isolation(provider):
    _seed(provider, "alice", ["alice note"])
    _seed(provider, "bob", ["bob note"])
    res = asyncio.run(provider.search("", user_id="alice"))
    assert "alice note" in res
    assert "bob note" not in res


def test_anon_blocked_returns_empty(provider):
    _seed(provider, "alice", ["secret data"])
    assert asyncio.run(provider.search("secret", user_id="")) == ""
    assert asyncio.run(provider.search("", user_id=None)) == ""


def test_anon_allowed_when_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    p = SqliteMemoryProvider(str(tmp_path / "m.db"), top_k=5)
    _seed(p, "", ["shared bucket widget"])
    assert "shared bucket" in asyncio.run(p.search("widget", user_id=""))


def test_prefetch_legacy_shape_unchanged(provider):
    # P2-1: seed in a DIFFERENT session than we prefetch from — prefetch now excludes
    # the current session (self-echo guard), so cross-session recall is the real shape.
    _seed(provider, "alice", ["widget alpha"], session="s0")
    # prefetch: rank-ordered, returns "" on no-terms, "" on anon
    assert "widget alpha" in asyncio.run(provider.prefetch("widget", session_id="s1", user_id="alice"))
    assert asyncio.run(provider.prefetch("", session_id="s1", user_id="alice")) == ""
    assert asyncio.run(provider.prefetch("a b", session_id="s1", user_id="alice")) == ""  # all <3 chars
    assert asyncio.run(provider.prefetch("widget", session_id="s1", user_id="")) == ""


# --------------------------------------------------------------------------------- #
# T2.6 — before_id pagination + with_ids display
# --------------------------------------------------------------------------------- #

def test_with_ids_default_false_keeps_legacy_format(provider):
    """with_ids defaults False -> byte-identical "- {content}" shape, no id tags."""
    _seed(provider, "alice", ["widget alpha"])
    out = asyncio.run(provider.search("widget", user_id="alice"))
    assert "(id" not in out


def test_with_ids_true_appends_id_tags(provider):
    _seed(provider, "alice", ["widget alpha", "widget beta"])
    out = asyncio.run(provider.search("widget", user_id="alice", limit=2, with_ids=True))
    assert len(_ids(out)) == 2


def test_before_id_pagination_newest_no_overlap_strictly_older(provider):
    _seed(provider, "alice", [f"widget {i}" for i in range(6)])
    page1 = asyncio.run(provider.search("widget", user_id="alice", limit=2,
                                        sort="newest", with_ids=True))
    ids1 = _ids(page1)
    assert len(ids1) == 2
    page2 = asyncio.run(provider.search("widget", user_id="alice", limit=2,
                                        sort="newest", before_id=min(ids1),
                                        with_ids=True))
    ids2 = _ids(page2)
    assert len(ids2) == 2
    # No overlap between pages, and page 2 is strictly older (smaller rowid).
    assert set(ids1).isdisjoint(ids2)
    assert all(i < min(ids1) for i in ids2)
    # Continuing to page 3 keeps going strictly older still.
    page3 = asyncio.run(provider.search("widget", user_id="alice", limit=2,
                                        sort="newest", before_id=min(ids2),
                                        with_ids=True))
    ids3 = _ids(page3)
    assert ids3 and all(i < min(ids2) for i in ids3)


def test_before_id_pagination_rank_sort_keeps_rank_order_and_filters(provider):
    """Default (rank) sort + before_id: the WHERE filter narrows candidates, the
    ORDER BY stays rank — NOT switched to rowid order."""
    _seed(provider, "alice", [f"widget {i}" for i in range(5)])
    full = asyncio.run(provider.search("widget", user_id="alice", limit=20, with_ids=True))
    all_ids = _ids(full)
    assert len(all_ids) == 5
    cutoff = sorted(all_ids)[-1]  # exclude only the single largest rowid
    filtered = asyncio.run(provider.search("widget", user_id="alice", limit=20,
                                           before_id=cutoff, with_ids=True))
    filtered_ids = _ids(filtered)
    assert cutoff not in filtered_ids
    assert len(filtered_ids) == 4
    # The relative order among the surviving rows is unchanged from the unfiltered
    # rank-ordered result (before_id only removed a candidate, didn't re-sort).
    assert filtered_ids == [i for i in all_ids if i != cutoff]


def test_before_id_pagination_oldest_sort_documented_behavior(provider):
    """T2.6 explicitly does NOT invent forward-pagination semantics for
    sort="oldest" — before_id is the SAME `rowid < before_id` filter applied
    under ascending order, i.e. it narrows to the OLDER end again, not the next
    page past what was already shown. Pinning the actual (documented) behavior."""
    _seed(provider, "alice", [f"widget {i}" for i in range(6)])
    page1 = asyncio.run(provider.search("widget", user_id="alice", limit=2,
                                        sort="oldest", with_ids=True))
    ids1 = _ids(page1)
    assert ids1 == sorted(ids1)  # ascending
    cursor = max(ids1)  # the largest id seen so far on this (oldest-first) page
    page2 = asyncio.run(provider.search("widget", user_id="alice", limit=2,
                                        sort="oldest", before_id=cursor,
                                        with_ids=True))
    ids2 = _ids(page2)
    # Every returned id is strictly below the cursor (the filter, honestly applied)
    # — for "oldest" this means page2 re-covers ground BELOW the cursor, including
    # ids already seen on page1, rather than advancing forward. That is the
    # documented, non-invented behavior — not a bug.
    assert all(i < cursor for i in ids2)


def test_before_id_clamp_interplay(provider):
    """limit clamping [1,20] still applies with before_id set."""
    _seed(provider, "alice", [f"widget {i}" for i in range(25)])
    full = asyncio.run(provider.search("widget", user_id="alice", limit=999, with_ids=True))
    all_ids = _ids(full)
    assert len(all_ids) == 20  # clamped
    res = asyncio.run(provider.search("widget", user_id="alice", limit=999,
                                      before_id=min(all_ids) + 1, with_ids=True))
    assert len(_ids(res)) <= 20


# --------------------------------------------------------------------------------- #
# T2.6 — automation-source demotion (rank-sorted queries only)
# --------------------------------------------------------------------------------- #

def test_automation_session_demoted_below_interactive_on_rank(provider):
    """A 'cron'-tagged session's row sorts AFTER an interactive one even when the
    automation row has objectively stronger raw FTS5 relevance (more term hits) —
    proves demotion actually overrides plain rank, not just a coincidental order."""
    asyncio.run(provider.sync_turn("q", "widget report", session_id="s_chat", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget widget widget urgent",
                                   session_id="s_cron", user_id="alice"))
    _tag_episode(provider, session_id="s_cron", user_id="alice", kind="cron")
    out = asyncio.run(provider.search("widget", user_id="alice"))
    assert out.index("widget report") < out.index("widget widget widget urgent")


def test_goal_session_also_demoted(provider):
    asyncio.run(provider.sync_turn("q", "widget report", session_id="s_chat", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget widget widget urgent",
                                   session_id="s_goal", user_id="alice"))
    _tag_episode(provider, session_id="s_goal", user_id="alice", kind="goal")
    out = asyncio.run(provider.search("widget", user_id="alice"))
    assert out.index("widget report") < out.index("widget widget widget urgent")


def test_no_episode_data_is_a_noop(provider):
    """Without any episodes rows (EPISODIC_MEMORY_ENABLED off, the default), the
    demotion subquery is always false -> plain rank order, unchanged from before
    T2.6 — the stronger-relevance row still ranks first."""
    asyncio.run(provider.sync_turn("q", "widget report", session_id="s_chat", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget widget widget urgent",
                                   session_id="s_other", user_id="alice"))
    out = asyncio.run(provider.search("widget", user_id="alice"))
    assert out.index("widget widget widget urgent") < out.index("widget report")


def test_chat_episode_not_demoted(provider):
    """A 'chat'-kind episode is interactive, not automation — no demotion."""
    asyncio.run(provider.sync_turn("q", "widget report", session_id="s_chat1", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget widget widget urgent",
                                   session_id="s_chat2", user_id="alice"))
    _tag_episode(provider, session_id="s_chat2", user_id="alice", kind="chat")
    out = asyncio.run(provider.search("widget", user_id="alice"))
    assert out.index("widget widget widget urgent") < out.index("widget report")


def test_demotion_scoped_to_rank_sort_not_newest(provider):
    """sort="newest" is an explicit time-order request — demotion must NOT apply
    (an automation row written most-recently still sorts first)."""
    asyncio.run(provider.sync_turn("q", "widget report", session_id="s_chat", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget cron finding",
                                   session_id="s_cron", user_id="alice"))
    _tag_episode(provider, session_id="s_cron", user_id="alice", kind="cron")
    out = asyncio.run(provider.search("widget", user_id="alice", sort="newest"))
    # s_cron was written SECOND -> newest -> must appear first despite being automation.
    assert out.index("widget cron finding") < out.index("widget report")


def test_demotion_is_tenant_scoped(provider):
    """A 'cron' episode belonging to a DIFFERENT tenant, whose session_id string
    happens to collide with one of THIS tenant's rows, must not demote it
    (review fix: the EXISTS subquery requires e.user_id = m.user_id — session_id
    is not guaranteed globally unique across tenants)."""
    asyncio.run(provider.sync_turn("q", "widget widget widget urgent",
                                   session_id="s_collide", user_id="alice"))
    asyncio.run(provider.sync_turn("q", "widget report",
                                   session_id="s_alice_other", user_id="alice"))
    # Bob owns the episode tagging the SAME session_id string as cron.
    asyncio.run(provider.sync_turn("q", "bob's own widget note",
                                   session_id="s_collide", user_id="bob"))
    _tag_episode(provider, session_id="s_collide", user_id="bob", kind="cron")
    out = asyncio.run(provider.search("widget", user_id="alice"))
    # Alice's own row keeps plain rank order — bob's cron tag on a colliding
    # session_id string (a different tenant) must not leak into her ordering.
    assert out.index("widget widget widget urgent") < out.index("widget report")
