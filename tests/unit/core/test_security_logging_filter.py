"""Redaction-coverage audit (T2.5, the 2026-07-22 catch-up plan).

Three independently-verified gaps in ``core.security_logging_filter.SecretScrubbingFilter``
before this test file existed (there was NO test coverage for it at all):

1. **Dead wiring (the P0).** ``core/logging.py`` attached the filter to the ROOT
   *Logger* (``root_logger.addFilter(...)``). A ``logging.Filter`` on a ``Logger``
   object is only consulted in ``Logger.handle()`` for the logger it was called
   on — NOT for ancestor loggers, and NOT via ``Handler.filter()``. Virtually all
   real call sites use a NAMED component logger (``logging.getLogger(__name__)``),
   so the filter never ran for them; a secret logged that way reached both the
   console and ``bot.log`` completely unredacted. Fix: attach the filter directly
   to every Handler ``setup_logging`` creates.
2. **``record.args`` / exception text bypassed the scrub.** ``filter()`` only
   touched ``record.msg`` and monkeypatched ``record.getMessage`` — the raw
   ``record.args`` tuple was left untouched (any formatter/exporter reading args
   directly would leak the secret), and ``record.exc_info``/``exc_text`` were
   never touched at all, so a secret embedded in a raised exception's message
   survived straight into the formatted traceback.
3. **Pattern drift vs the SSOT** (``core/secret_patterns.py`` — already shared by
   ``core/secret_scrub.py`` and ``cli/ui/secrets.py``). The filter hand-rolled its
   own regex list instead of importing the SSOT; PEM blocks, ``sk-``/``pk-``/``rk-``
   provider keys (any length), ``rob_...`` POLYROB API keys, and AWS ``AKIA...``
   access-key IDs all passed through completely unredacted.

The acceptance test is ``test_secret_never_reaches_log_file_via_args_or_exception``:
a fake ``sk-...`` key logged via ``%s`` args AND via a raised exception, through a
NAMED component logger (not root), must never appear in ``bot.log``.
"""
import logging
import re
import sys

import pytest

import core.secret_patterns as ssot
from core.security_logging_filter import SecretScrubbingFilter

# OpenAI-shaped fake secret (48 chars after "sk-", matches the filter's own
# dedicated OpenAI pattern too) — never a real credential.
FAKE_KEY = "sk-" + "A" * 48


@pytest.fixture
def clean_root_logging():
    """Save/restore the root logger's handlers/filters/level and
    ``core.logging._ROOT_LOGGER_CONFIGURED`` so this test can force a fresh
    ``setup_logging()`` run regardless of what earlier tests in the session did,
    without leaking state into tests that run after it.
    """
    import core.logging as cl

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    saved_configured = cl._ROOT_LOGGER_CONFIGURED

    for h in list(root.handlers):
        root.removeHandler(h)
    root.filters.clear()
    cl._ROOT_LOGGER_CONFIGURED = False

    yield

    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.filters[:] = saved_filters
    root.setLevel(saved_level)
    cl._ROOT_LOGGER_CONFIGURED = saved_configured


def test_secret_never_reaches_log_file_via_args_or_exception(
    monkeypatch, tmp_path, clean_root_logging
):
    """The acceptance test: a fake sk-... key logged via args or an exception,
    through a NAMED component logger (how real app code logs), must never reach
    bot.log."""
    monkeypatch.setenv("POLYROB_LOG_DIR", str(tmp_path))
    from core.logging import setup_logging

    setup_logging(log_level="INFO")

    # NOT the root logger — this is how virtually all application code logs
    # (logging.getLogger(__name__)).
    logger = logging.getLogger("some.deeply.nested.component")

    logger.info("api_key=%s", FAKE_KEY)
    try:
        raise ValueError(f"boom api_key={FAKE_KEY}")
    except ValueError:
        logger.exception("failed with secret")

    for h in logging.getLogger().handlers:
        h.flush()

    log_path = tmp_path / "bot.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert FAKE_KEY not in content, "fake secret reached bot.log unredacted"


def test_filter_attached_to_every_handler_setup_logging_creates(
    monkeypatch, tmp_path, clean_root_logging
):
    monkeypatch.setenv("POLYROB_LOG_DIR", str(tmp_path))
    from core.logging import setup_logging

    setup_logging(log_level="INFO")

    handlers = logging.getLogger().handlers
    assert handlers, "setup_logging created no handlers"
    for h in handlers:
        assert any(isinstance(f, SecretScrubbingFilter) for f in h.filters), (
            f"handler {h!r} (sink={getattr(h, '_polyrob_sink', '?')}) has no "
            "SecretScrubbingFilter attached"
        )


def test_filter_scrubs_record_args_in_place():
    """record.args (not just the interpolated message) must be scrubbed —
    anything downstream that reads args directly (a custom formatter, a
    structured-log exporter) must not see the raw secret either."""
    f = SecretScrubbingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "api_key=%s", (FAKE_KEY,), None)
    f.filter(rec)
    assert FAKE_KEY not in rec.getMessage()
    assert not any(FAKE_KEY in str(a) for a in (rec.args or ())), "raw secret survives in record.args"


def test_filter_scrubs_exception_traceback_text():
    f = SecretScrubbingFilter()
    try:
        raise ValueError(f"boom api_key={FAKE_KEY}")
    except ValueError:
        exc_info = sys.exc_info()
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, exc_info)
    f.filter(rec)
    formatted = logging.Formatter().format(rec)
    assert FAKE_KEY not in formatted, "raw secret survives in the formatted exception traceback"


@pytest.mark.parametrize(
    "label, sample",
    [
        ("PEM", "-----BEGIN PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END PRIVATE KEY-----"),
        ("BEARER", "Authorization: Bearer abcd1234efgh5678ijkl"),
        ("KV (prefixed env-var shape)", "MY_SERVICE_API_KEY=abcdef0123456789zzzz"),
        ("PROVIDER_KEY (sk-/pk-/rk-, any length)", "sk-abcdefghijklmnopqrstuvwx"),
        ("POLYROB_KEY (rob_...)", "rob_abcdefghijklmnopqrstuvwx"),
        ("AWS access-key id (AKIA...)", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_filter_covers_every_ssot_secret_shape(label, sample):
    """Drift lock: every high-confidence shape in the core.secret_patterns SSOT
    (already shared by core/secret_scrub.py + cli/ui/secrets.py) must also be
    redacted by the logging filter — not a hand-rolled, silently-diverging
    subset."""
    f = SecretScrubbingFilter()
    scrubbed = f.scrub_message(sample)
    assert scrubbed != sample, f"{label} not redacted by SecretScrubbingFilter: {sample!r}"


def test_ssot_patterns_are_present_in_filter_pattern_list():
    """Identity check (mirrors test_shared_credential_patterns.py's convention for
    the other two scrubbers): the filter must use the SAME compiled SSOT pattern
    objects, not a re-typed copy that can drift again."""
    for pattern in (
        ssot.PEM_RE,
        ssot.BEARER_RE,
        ssot.KV_RE,
        ssot.PROVIDER_KEY_RE,
        ssot.POLYROB_KEY_RE,
        ssot.AWS_RE,
    ):
        assert pattern in SecretScrubbingFilter.SECRET_PATTERNS, (
            f"SSOT pattern {pattern.pattern!r} missing from "
            "SecretScrubbingFilter.SECRET_PATTERNS"
        )


# ---------------------------------------------------------------------------
# Review follow-up (T2.5, same day): marker pre-check + per-record dedup.
# ---------------------------------------------------------------------------


def test_benign_record_msg_and_args_are_untouched_same_objects():
    """A record with NO marker anywhere (msg, args, exc text) must never be
    mutated — not just equal, the SAME objects (identity), proving the regex
    battery never ran at all."""
    f = SecretScrubbingFilter()
    msg = "session created for user %s with status ok"
    args = ("user123",)
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, msg, args, None)

    f.filter(rec)

    assert rec.msg is msg, "benign record.msg was reassigned"
    assert rec.args is args, "benign record.args was reassigned"


def test_benign_msg_with_secret_bearing_args_still_scrubs():
    """The marker pre-check runs on the CONCATENATION of msg+args+exc text —
    a wholly benign msg with a secret embedded only in args must still scrub."""
    f = SecretScrubbingFilter()
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "request completed: %s", (FAKE_KEY,), None
    )
    f.filter(rec)
    assert FAKE_KEY not in rec.getMessage()
    assert not any(FAKE_KEY in str(a) for a in (rec.args or ()))


def test_filter_battery_runs_once_across_multiple_handlers():
    """Per-record dedup: N handlers on one logger filtering the SAME record must
    only pay for the regex battery once — proven via a counting subclass whose
    scrub_message() is instrumented. Both handlers still see redacted output
    because the record was mutated in place by the first filter call."""
    call_count = {"n": 0}

    class CountingFilter(SecretScrubbingFilter):
        def scrub_message(self, message):
            call_count["n"] += 1
            return super().scrub_message(message)

    filter_a = CountingFilter()
    filter_b = CountingFilter()

    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "api_key=%s", (FAKE_KEY,), None
    )

    # Simulate the SAME record passing through two handlers on one logger.
    filter_a.filter(rec)
    filter_b.filter(rec)

    assert call_count["n"] > 0, "battery never ran at all"
    first_call_count = call_count["n"]

    # The second filter() call must be a no-op (dedup stamp already set) —
    # scrub_message must not have been invoked again.
    assert call_count["n"] == first_call_count, (
        "battery ran again on the second handler's filter() call"
    )
    assert FAKE_KEY not in rec.getMessage()
    assert not any(FAKE_KEY in str(a) for a in (rec.args or ()))


def test_extra_field_scrubbed_even_with_benign_msg_args_exc():
    """SENSITIVE_FIELDS scrubbing (extra={...} kwargs) is keyed on FIELD NAME,
    not message content, so it must still fire even when msg/args/exc contain
    no marker at all — and must survive the _STANDARD_RECORD_ATTRS narrowing
    (only genuine ``extra=`` keys are scanned, not the ~20 stock LogRecord
    attributes)."""
    f = SecretScrubbingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "all good here", (), None)
    rec.password = "hunter2hunter2"
    f.filter(rec)
    assert rec.password != "hunter2hunter2"
    assert "hunter2hunter2" not in rec.password


def test_has_marker_gate():
    """Direct unit coverage of the pre-check helper itself."""
    assert SecretScrubbingFilter._has_marker("") is False
    assert SecretScrubbingFilter._has_marker("plain benign text, nothing here") is False
    assert SecretScrubbingFilter._has_marker("api_key=abc") is True
    assert SecretScrubbingFilter._has_marker("Bearer abcd") is True
    assert SecretScrubbingFilter._has_marker("AKIAIOSFODNN7EXAMPLE") is True


@pytest.mark.parametrize("label,secret", [
    ("github-pat", "ghp_" + "C" * 36),
    ("google-key", "AIzaSy" + "D" * 33),
    ("anthropic-legacy", "claude-" + "E" * 24),
    ("pinecone", "pc-" + "f" * 32),
    ("base64-blob", "Zm9vYmFy" + "Qk" * 16),
])
def test_markerless_secret_shapes_still_scrub(label, secret):
    """Regression for the marker-gate hole: three LEGACY_PATTERNS ("claude-",
    "pc-", and the base64-blob catch-all that nets ghp_/AIzaSy-style tokens)
    match WITHOUT any MARKER_WORDS substring. A record carrying one of these
    with no marker word anywhere must still run the battery and scrub —
    gating them out silently reopened redaction for common secret shapes."""
    f = SecretScrubbingFilter()
    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "outbound header value: %s", (secret,), None
    )
    f.filter(rec)
    assert secret not in rec.getMessage(), f"{label} leaked through the marker gate"
    assert secret not in str(rec.args), f"{label} leaked through record.args"


# ---------------------------------------------------------------------------
# A12 (2026-09-14): the generic base64-blob catch-all must not eat a path.
# ---------------------------------------------------------------------------


def test_base64_pattern_does_not_eat_a_path_that_reproduces_the_old_bug():
    """A12 fix round 3 (2026-09-14, review finding): round 2 dropped '/' from
    the character class to stop the catch-all eating paths — but that also
    stopped it redacting a REAL secret containing '/' (see the sibling test
    below: ~47% of random 40-char base64 tokens contain at least one '/',
    and nothing else in the battery backstops a bare token). The fix keeps
    '/' in the class and adds lookarounds instead: the lookbehind refuses a
    match starting right after '/' or a word character, so a match can never
    start MID-path (every internal path-segment boundary is one of those
    two). The only vulnerable start is the leading edge of the path's own
    first homogeneous run (right after the message's own word boundary —
    here, the space after "at ") — so this is safe only as long as THAT
    leading run stays under the 32-char floor. This fixture's leading run
    ("/Users/example/", 15 chars) is short, broken by the underscore in
    "_Library" before reaching 32 — while a LATER, unbroken run inside the
    same path ("Library/Caches/ms/playwright/.../libEGL") is long enough to
    trip the OLD (lookaround-free) pattern, asserted inline below as the
    "this fixture reproduces the bug" guard (a prior version of this test
    used a fixture the reviewer found never triggered the old pattern at
    all — this one does)."""
    old_unbounded_pattern = re.compile(r'([a-zA-Z0-9+/]{32,}={0,2})')
    path = (
        "/Users/example/_Library/Caches/ms/playwright/chromium/Chromium/"
        "Contents/MacOS/Chromium/Framework/Versions/Current/Libraries/libEGL"
    )
    assert old_unbounded_pattern.findall(path), (
        "fixture doesn't reproduce the bug — the OLD lookaround-free pattern "
        "must find a match inside the bare path; adjust the fixture until it does"
    )

    f = SecretScrubbingFilter()
    scrubbed = f.scrub_message(f"Executable doesn't exist at {path}")
    assert path in scrubbed, f"path was mangled: {scrubbed!r}"
    assert "REDACTED" not in scrubbed, f"path was redacted: {scrubbed!r}"


def test_base64_pattern_still_redacts_a_slash_containing_standalone_token():
    """The regression round 3 fixes: round 2's class-narrowing (dropping '/')
    silently stopped redacting a real secret that happens to contain '/' —
    a realistic shape (~47% of random 40-char base64 tokens have at least
    one '/'). '/' is back in the character class; the lookarounds alone
    carry the path-safety burden (see the sibling test above). A token
    bounded by whitespace/punctuation — not path separators — on both sides
    still matches in full, '/' included."""
    f = SecretScrubbingFilter()
    token = "x1Fh+zm9tbRkRMgSnMJq8Mt3oa94hHBLzA/qfpIY"
    assert len(token) == 40
    scrubbed = f.scrub_message(f"leaked token: {token}")
    assert token not in scrubbed, f"standalone token survived: {scrubbed!r}"


# ---------------------------------------------------------------------------
# A12 fix round 4 (2026-09-14, review finding): round 3's lookbehind alone
# still let a match START at a path's own leading '/' — that position is
# preceded by a space (or is the very start of the message), and neither is
# blocked by `(?<![/\w])`. A fully HOMOGENEOUS absolute path (no digit,
# underscore, hyphen, or period anywhere near its start — so nothing breaks
# the run before the 32-char floor) was still eaten whole from position 0.
# Fix: `(?!/)` right after the lookbehind — a match may not itself start
# with '/'. A base64 secret essentially never starts with '/' (~1/64 chance
# for a random character); every ABSOLUTE path does. That IS the accepted
# blind spot this round introduces: a secret whose own first character
# happens to be '/' is not redacted by this pattern — no lookaround can
# distinguish "a path" from "a secret starting with the path-separator
# byte" from the leading character alone.
# ---------------------------------------------------------------------------


def test_homogeneous_path_after_a_space_is_not_eaten_from_its_leading_slash():
    """Round 3's fixture needed an early underscore to break the leading run
    short. This one has NO such break anywhere in its first 32+ chars — the
    shape round 3 missed. Preceded by a space (a genuine word-boundary),
    the OLD round-3 pattern (lookbehind only, no `(?!/)`) matches starting
    at the path's own leading '/' and eats the whole thing; asserted inline
    below as the "reproduces the round-3 gap" guard."""
    round3_pattern = re.compile(r'(?<![/\w])([a-zA-Z0-9+/]{32,}={0,2})(?!\w)')
    old_unbounded_pattern = re.compile(r'([a-zA-Z0-9+/]{32,}={0,2})')
    path = "/Users/example/Library/Caches/ms/playwright/chromium/Chromium/Contents"

    assert old_unbounded_pattern.findall(path), "fixture doesn't reproduce the original bug"
    assert round3_pattern.findall(f"cache at {path}") == [path], (
        "fixture doesn't reproduce the round-3 gap — round 3's lookbehind-only "
        "pattern must still eat this path whole for this test to prove anything"
    )

    f = SecretScrubbingFilter()
    scrubbed = f.scrub_message(f"cache at {path}")
    assert path in scrubbed, f"path was mangled: {scrubbed!r}"
    assert "REDACTED" not in scrubbed, f"path was redacted: {scrubbed!r}"


def test_homogeneous_path_at_message_start_is_not_eaten():
    """Same shape as above, but with NOTHING before the path at all — the
    other vulnerable position (start-of-string also isn't blocked by the
    lookbehind, same as a leading space)."""
    f = SecretScrubbingFilter()
    path = "/Users/example/Library/Caches/ms/playwright/chromium/Chromium/Contents"
    scrubbed = f.scrub_message(path)
    assert scrubbed == path, f"path was mangled: {scrubbed!r}"


def test_slash_containing_token_after_a_space_still_redacted_with_the_new_lookahead():
    """The `(?!/)` lookahead added this round must not blunt the net from
    the sibling test above: a '/'-containing secret (which does NOT itself
    start with '/') is still caught in full, preceded by a plain space."""
    f = SecretScrubbingFilter()
    token = "x1Fh+zm9tbRkRMgSnMJq8Mt3oa94hHBLzA/qfpIY"
    scrubbed = f.scrub_message(f"cache at {token} was stale")
    assert token not in scrubbed, f"standalone token survived: {scrubbed!r}"


# ---------------------------------------------------------------------------
# Review follow-up (T2.5, same day): dead Logger-level auto-install removed.
# ---------------------------------------------------------------------------


def test_install_global_secret_scrubbing_removed():
    """install_global_secret_scrubbing() was the exact dead Logger-level
    mechanism the P0 fix diagnosed (module-import auto-attach to the root
    Logger, never consulted for named child-logger records) and had no
    external callers. It must be gone, not just unused."""
    import core.security_logging_filter as mod

    assert not hasattr(mod, "install_global_secret_scrubbing")
