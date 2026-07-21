"""2026-07-19 fix: the `message` action's ActionResult carried no content signal
for the completion judge to verify against, and never set `error` on a failed
send either (a genuinely failed send looked identical to a success in the
judge's evidence ledger — agents/task/runtime/evidence.py builds straight from
ActionResult.extracted_content/error). Live-observed: a goal whose acceptance
criteria referenced the message's CONTENT was rejected as "no successful
message action... with the changelog content recorded" despite the send
genuinely succeeding (confirmed via the raw tool-call log) — the evidence pack
had no content signal to match against at all.
"""
from tools.controller.action_registration import _message_action_result


def test_success_includes_a_text_preview():
    res = _message_action_result(
        {"success": True, "tier": "owner"}, "telegram", "owner", "hello owner")
    assert res.error is None
    assert "OK" in res.extracted_content
    assert "hello owner" in res.extracted_content


def test_success_preview_is_bounded_and_marked_truncated():
    long_text = "x" * 500
    res = _message_action_result(
        {"success": True, "tier": "owner"}, "telegram", "owner", long_text)
    assert "x" * 200 in res.extracted_content
    assert "x" * 201 not in res.extracted_content
    assert "…" in res.extracted_content


def test_failure_sets_a_real_error_not_just_text():
    """Before the fix, `error` was never set on a failed send — a genuine
    failure looked identical to a success in the evidence ledger."""
    res = _message_action_result(
        {"success": False, "tier": "denied", "error": "target not on owner allowlist"},
        "telegram", "someone", "hi")
    assert res.error == "target not on owner allowlist"
    assert "FAILED" in res.extracted_content
    assert "target not on owner allowlist" in res.extracted_content


def test_failure_without_explicit_error_still_sets_a_nonempty_error():
    res = _message_action_result({"success": False, "tier": "denied"}, "telegram", "x", "hi")
    assert res.error
    assert "FAILED" in res.extracted_content


def test_note_is_preserved_on_both_paths():
    ok = _message_action_result(
        {"success": True, "tier": "open", "note": "seeded new correspondent"},
        "telegram", "x", "hi")
    assert "[seeded new correspondent]" in ok.extracted_content
    fail = _message_action_result(
        {"success": False, "tier": "open", "error": "cap reached", "note": "daily cap"},
        "telegram", "x", "hi")
    assert "[daily cap]" in fail.extracted_content
