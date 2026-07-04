"""A2 — STRIP_BASE64_IMAGES 'anchor' mode.

The blunt ``STRIP_BASE64_IMAGES=true`` regex-strips base64 from text at parse time.
'anchor' mode turns that OFF so the anchor-preserving ``strip_historical_media``
pass (already wired into get_messages_for_llm) becomes the bounding path — the
latest image-bearing turn survives, older base64 is stripped. Default stays 'true'
to avoid a vision regression.

``resolve_base64_strip_mode`` is the pure resolver under test.
"""
from agents.task.robust_parse_config import (
    RobustParseConfig,
    resolve_base64_strip_mode,
)


def test_default_true_strips_at_parse_not_anchor():
    r = resolve_base64_strip_mode("true")
    assert r["strip_at_parse"] is True
    assert r["anchor"] is False


def test_unknown_value_defaults_to_blunt_strip():
    # Any unrecognized value preserves the current (safe) blunt behaviour.
    r = resolve_base64_strip_mode("yes")
    assert r["strip_at_parse"] is True
    assert r["anchor"] is False


def test_false_strips_nothing_and_not_anchor():
    r = resolve_base64_strip_mode("false")
    assert r["strip_at_parse"] is False
    assert r["anchor"] is False


def test_anchor_mode_disables_parse_strip_and_sets_anchor():
    r = resolve_base64_strip_mode("anchor")
    assert r["strip_at_parse"] is False
    assert r["anchor"] is True


def test_anchor_mode_is_case_and_whitespace_insensitive():
    r = resolve_base64_strip_mode("  Anchor ")
    assert r["strip_at_parse"] is False
    assert r["anchor"] is True


def test_config_exposes_anchor_mode_flag():
    # The class surfaces an explicit anchor flag derived from the resolver.
    assert hasattr(RobustParseConfig, "STRIP_BASE64_ANCHOR_MODE")
    assert isinstance(RobustParseConfig.STRIP_BASE64_ANCHOR_MODE, bool)
