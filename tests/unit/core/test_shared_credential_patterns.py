"""Regression (P4 finalization): the six high-confidence credential patterns are now
defined once in core.secret_patterns and imported by BOTH scrubbers, so the _KV_RE
<PREFIX>_API_KEY= divergence bug can't recur. This asserts they are the SAME objects.
"""
import core.secret_patterns as shared
import core.secret_scrub as persisted
import cli.ui.secrets as display


def test_both_scrubbers_use_the_shared_pattern_objects():
    pairs = [
        ("_PEM_RE", shared.PEM_RE),
        ("_BEARER_RE", shared.BEARER_RE),
        ("_KV_RE", shared.KV_RE),
        ("_PROVIDER_KEY_RE", shared.PROVIDER_KEY_RE),
        ("_ROB_KEY_RE", shared.POLYROB_KEY_RE),
        ("_AWS_RE", shared.AWS_RE),
    ]
    for name, obj in pairs:
        assert getattr(persisted, name) is obj, f"persisted scrubber must share {name}"
        assert getattr(display, name) is obj, f"display scrubber must share {name}"


def test_scrubbers_keep_distinct_redaction_markers():
    # They share PATTERNS but not the marker (persisted uses a tagged shape).
    assert persisted.REDACTED != display.REDACTED
