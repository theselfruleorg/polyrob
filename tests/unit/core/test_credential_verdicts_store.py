"""057 WS-F: an external-rail verdict is DURABLE.

Before WS-F the register was a process-local dict keyed on ``time.monotonic()``:
a restart forgot every verdict and re-probed every dead rail, and the three
service units that share one data dir each kept a private copy. The store is a
SQLite table under the data home, so a restart (simulated here by clearing the
module's caches, which is all a fresh process has) still knows the rail is down
and still knows WHEN it went down.
"""
import sqlite3
import time

import pytest

from core import credential_verdicts as cv


def _restart():
    """What a new process sees: empty in-memory caches, the same db file."""
    cv._FALLBACK.clear()
    cv._WARNED.clear()
    cv._READY.clear()
    cv._DEGRADED_LOGGED = False


def test_verdict_survives_a_restart():
    cv.record_rejection("smtp", "smtp.test:587:bot@example.com", code="535",
                        remedy="fix the app password")
    _restart()
    v = cv.verdict("smtp", "smtp.test:587:bot@example.com")
    assert v is not None
    assert v.code == "535"
    assert v.remedy == "fix the app password"
    assert cv.rejected_within("smtp", cv.SMTP_TTL_SEC)


def test_repeat_keeps_first_seen_and_counts():
    cv.record_rejection("twitter_api", "post", code="402")
    first = cv.verdict("twitter_api", "post").first_seen
    time.sleep(0.01)
    second = cv.record_rejection("twitter_api", "post")
    assert second.count == 2
    assert second.first_seen == pytest.approx(first)
    assert second.last_seen > first
    # a fresh verdict is identifiable by count == 1 — that is the "emit the
    # durable event once, not per refused call" seam.
    assert cv.record_rejection("twitter_api", "search").count == 1


def test_success_clears_the_row():
    cv.record_rejection("smtp", "a")
    cv.clear_rejection("smtp", "a")
    assert cv.verdict("smtp", "a") is None
    assert not cv.rejected_within("smtp", 10_000)
    # and the NEXT failure starts a new episode
    time.sleep(0.01)
    again = cv.record_rejection("smtp", "a")
    assert again.count == 1


def test_ttl_expiry_is_liveness_not_deletion():
    # A 50 ms budget spanning two sqlite round-trips is a coin flip on a loaded
    # shared runner — it went red in public CI and green on every dev machine.
    # The property under test is liveness-vs-deletion, not resolution.
    cv.record_rejection("smtp", "a", ttl_sec=0.5)
    assert cv.verdict("smtp", "a").live
    time.sleep(0.7)
    v = cv.verdict("smtp", "a")
    assert not v.live, "the re-probe backoff lapsed"
    assert not cv.rejected_within("smtp", 0.5)
    # an unresolved rail is still REPORTED — only a success clears it
    assert [x.key for x in cv.active("smtp")] == ["a"]
    assert cv.active("smtp", live_only=True) == []


def test_default_ttl_comes_from_the_kind():
    assert cv.record_rejection("smtp", "x").ttl_sec == cv.SMTP_TTL_SEC
    assert cv.record_rejection("twitter_api", "x").ttl_sec == cv.TWITTER_API_TTL_SEC
    assert cv.record_rejection("missing_key", "perplexity").ttl_sec == cv.MISSING_KEY_TTL_SEC
    # an unknown kind has no TTL, which means it never lapses
    assert cv.record_rejection("odd", "x").ttl_sec is None
    assert cv.verdict("odd", "x").live


def test_active_filters_by_kind_and_orders_newest_first():
    cv.record_rejection("smtp", "a")
    time.sleep(0.01)
    cv.record_rejection("twitter_api", "post")
    kinds = [v.kind for v in cv.active()]
    assert kinds == ["twitter_api", "smtp"]
    assert [v.kind for v in cv.active("smtp")] == ["smtp"]


def test_since_text_reads_from_first_seen():
    cv.record_rejection("smtp", "a")
    v = cv.verdict("smtp", "a")
    text = cv.since_text(v)
    assert text.endswith("(0s)")
    assert time.strftime("%m-%d", time.gmtime(v.first_seen)) in text
    assert cv.duration_text(8 * 3600) == "8h"
    assert cv.duration_text(5 * 86400) == "5d"
    assert cv.duration_text(90) == "1m"


def test_warn_once_is_once_per_process_per_episode():
    assert cv.warn_once("smtp", "a", episode=100.0)
    assert not cv.warn_once("smtp", "a", episode=100.0)
    assert cv.warn_once("smtp", "a", episode=200.0), "a NEW outage logs again"


def test_unreadable_store_falls_open_to_process_memory(monkeypatch, caplog):
    """A verdict is an optimisation; a broken store must not fail a call closed."""
    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cv, "execute_retry", _boom)
    monkeypatch.setattr(cv, "init_schema", _boom)
    _restart()
    with caplog.at_level("WARNING", logger="core.credential_verdicts"):
        cv.record_rejection("smtp", "a", code="535")
        cv.record_rejection("smtp", "a")
    assert cv.rejected_within("smtp", 10.0)
    v = cv.verdict("smtp", "a")
    assert v.count == 2 and v.code == "535"
    assert [x.key for x in cv.active("smtp")] == ["a"]
    cv.clear_rejection("smtp", "a")
    assert not cv.rejected_within("smtp", 10.0)
    degraded = [r for r in caplog.records if "store unavailable" in r.message]
    assert len(degraded) == 1, "the degradation is logged ONCE, not per call"


def test_store_is_shared_across_two_openers(tmp_path, monkeypatch):
    """Three service units share one data dir — the second must see the first."""
    cv.record_rejection("smtp", "shared", code="535")
    path = cv._db()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT kind, key, code FROM verdicts").fetchall()
    finally:
        conn.close()
    assert rows == [("smtp", "shared", "535")]


# --- D68: a verdict only the OWNER can clear is never aged out --------------

def test_an_open_remedy_verdict_survives_the_retention_prune():
    """⚠️ The prune deleted any row idle for 30 days — which is EXACTLY what a
    standing, unfixed rejection looks like once the rail is dropped from the
    autonomous toolset and nothing re-probes it. The verdict vanished, the
    status line with it, and the rail silently came back into the autonomous
    toolset while the credential was still dead.
    """
    from core.sqlite_util import execute_retry

    old = time.time() - 60 * 86400
    cv.record_rejection("smtp", "mailbox", code="535")
    cv.record_rejection("selfhealing", "x", code="500")
    path = cv._db()
    execute_retry(path, "UPDATE verdicts SET last_seen = ?, first_seen = ?",
                  (old, old))
    cv.record_rejection("smtp", "other", code="535")   # any write prunes
    assert cv.verdict("smtp", "mailbox") is not None   # kept: owner must fix it
    assert cv.verdict("selfhealing", "x") is None      # aged out as before


def test_every_open_remedy_kind_has_a_ttl():
    for kind in cv.OPEN_REMEDY_KINDS:
        assert kind in cv.DEFAULT_TTL_BY_KIND, kind


def test_imap_is_a_first_class_kind():
    """D28: the RECEIVE half earns its own verdict, so an inbound outage has a
    SINCE clock instead of a quiet mailbox."""
    v = cv.record_rejection("imap", "imap.x:me@x", code="auth")
    assert v.ttl_sec == cv.IMAP_TTL_SEC
    assert cv.verdict("imap", "imap.x:me@x").count == 1
