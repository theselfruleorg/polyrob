"""A persistently revoked credential is probed once per BACKOFF period, not once per run.

Intel inbox 2026-09-19 23:00Z (056 WS4 follow-up, on top of 057 WS-F's durable store):
the hold was a flat 900 s while cron runs fire every 20–35 min, so a password revoked
for days was re-probed — and its traceback chain re-logged — on every run. The hold
now doubles per repeat failure up to a cap; and because a longer hold on a DURABLE
row would make the owner's fix wait, a verdict is keyed on a credential digest so a
new password has no standing verdict and is probed at once.
"""
import time

import pytest

from core import credential_verdicts as cv


def test_hold_doubles_per_repeat_failure_up_to_the_cap():
    assert cv.hold_sec(900, 1) == 900
    assert cv.hold_sec(900, 2) == 1800
    assert cv.hold_sec(900, 3) == 3600
    assert cv.hold_sec(900, 5) == 14400
    assert cv.hold_sec(900, 6) == cv.BACKOFF_CAP_SEC
    assert cv.hold_sec(900, 40) == cv.BACKOFF_CAP_SEC
    assert cv.hold_sec(None, 3) is None
    assert cv.hold_sec(0, 3) is None


def test_verdict_live_and_remaining_follow_the_grown_hold(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    for _ in range(3):
        v = cv.record_rejection("smtp", "s:587:u#d1", code="535")
    assert v.count == 3 and v.hold_sec == 3600
    now[0] += 3599
    v = cv.verdict("smtp", "s:587:u#d1")
    assert v.live and v.remaining_sec == pytest.approx(1.0)
    assert cv.rejected_within("smtp", cv.SMTP_TTL_SEC), "the BASE window still reads the grown hold"
    now[0] += 2
    v = cv.verdict("smtp", "s:587:u#d1")
    assert not v.live and v.remaining_sec == 0.0
    assert not cv.rejected_within("smtp", cv.SMTP_TTL_SEC)


def test_rejected_within_ignores_a_lapsed_first_failure(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    cv.record_rejection("smtp", "s:587:u#d1", code="535")
    now[0] += 901
    assert not cv.rejected_within("smtp", 900)


def test_success_clears_the_streak_so_the_next_hold_is_the_base(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    for _ in range(4):
        cv.record_rejection("smtp", "s:587:u#d1", code="535")
    cv.clear_rejection("smtp", "s:587:u#d1")
    v = cv.record_rejection("smtp", "s:587:u#d1", code="535")
    assert v.count == 1 and v.hold_sec == 900


def test_credential_digest_is_a_change_detector_not_a_reveal():
    d = cv.credential_digest("abcd-efgh-ijkl-mnop")
    assert len(d) == 12 and "abcd" not in d and "mnop" not in d
    assert cv.credential_digest("") == "" and cv.credential_digest(None) == ""
    assert d == cv.credential_digest("abcd-efgh-ijkl-mnop")
    assert d != cv.credential_digest("abcd-efgh-ijkl-mnoq")
