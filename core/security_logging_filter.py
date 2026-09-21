"""
Security logging filter to prevent secrets from being logged.

T2.5 (2026-07-23) redaction-coverage audit fixed three gaps here (see
``tests/unit/core/test_security_logging_filter.py`` for the acceptance tests):

1. This filter's own patterns had drifted from the high-confidence credential-shape
   SSOT (``core/secret_patterns.py``, already shared by ``core/secret_scrub.py`` and
   ``cli/ui/secrets.py``) — PEM blocks, ``sk-``/``pk-``/``rk-`` provider keys of any
   length, ``rob_...`` POLYROB keys, and AWS ``AKIA...`` access-key IDs all passed
   through unredacted. Fixed by importing the SSOT patterns into ``SECRET_PATTERNS``
   instead of maintaining a second, silently-diverging list.
2. ``filter()`` only scrubbed ``record.msg`` — the raw ``record.args`` tuple (the
   ``%s``-interpolation values) and any exception text (``record.exc_info``/
   ``exc_text``) were left completely untouched. Fixed: both are now scrubbed too.
3. The filter was wired onto the ROOT **Logger** object (``root_logger.addFilter``,
   in ``core/logging.py``), not onto its Handlers. A ``logging.Filter`` attached to a
   ``Logger`` is only consulted in that logger's OWN ``Logger.handle()`` call — never
   for records that originate on a named child logger (i.e. virtually every real
   ``logging.getLogger(__name__)`` call site) and never via ``Handler.filter()``. The
   filter was effectively dead for real application logging. Fixed in
   ``core/logging.py`` — the filter is now attached directly to every Handler.

Review follow-up (T2.5, same day) fixed two more issues once the filter above was
actually *live* on every Handler:

4. **Cost.** Running the full ~21-pattern battery (``scrub_message``) against every
   record's msg/args/exc-text was measured at roughly ~20x the cost of a benign
   record that contains no secret-shaped text at all, and — because the filter is now
   correctly attached to N handlers per logger — it re-ran the SAME battery once per
   handler for the SAME record. Fixed with two independent, composable mechanisms in
   ``filter()``: (a) a cheap marker pre-check (``_has_marker``) that skips the regex
   battery entirely unless the candidate text contains one of a short lowercase
   substring list every ``SECRET_PATTERNS`` entry requires somewhere in its match —
   a record with none of these markers anywhere in msg/args/exc-text is returned with
   msg/args as the SAME OBJECTS, untouched; (b) a per-record dedup stamp
   (``record._secret_scrubbed``) so N handlers sharing one record only pay for the
   battery once — the record is mutated in place, so every later handler already sees
   the scrubbed values.
5. **Dead Logger-level auto-install.** ``install_global_secret_scrubbing()`` used to
   run at *module import time* and attach the filter to the root **Logger** object —
   the exact dead mechanism diagnosed in point 3 above (a Logger-level filter never
   fires for child-logger records). It had no callers outside this module. Removed,
   along with the auto-install call. The one remaining Logger-level attachment is
   ``core/logging.py``'s ``root_logger.addFilter(security_filter)`` inside
   ``setup_logging`` — kept deliberately: unlike the deleted mechanism, it is
   documented there as narrow defense-in-depth for records that originate AT the root
   logger directly (bare ``logging.info(...)``), on top of (not instead of) the
   correct per-Handler attachment that actually covers named child loggers.
"""

import logging
import re
import traceback
from typing import Set

from core.secret_patterns import (
    REDACTED as _REDACTED,
    PEM_RE as _PEM_RE,
    BEARER_RE as _BEARER_RE,
    KV_RE as _KV_RE,
    PROVIDER_KEY_RE as _PROVIDER_KEY_RE,
    POLYROB_KEY_RE as _POLYROB_KEY_RE,
    AWS_RE as _AWS_RE,
    JWT_RE as _JWT_RE,
    PUBLIC_ADDRESS_RE as _PUBLIC_ADDRESS_RE,
    apply_ssot_shapes,
)

#: An EVM transaction / block hash (``0x`` + EXACTLY 64 hex) and a canonical
#: UUID. Both are PUBLIC identifiers: a receipt hash is the proof a money verb
#: produced, a UUID is a session / request / tool-call id. Neither is ever a
#: credential, and a log line that masks them loses the only handle an operator
#: has on the event he is reading.
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UUID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})$"
)


def _is_public_identifier(token: str) -> bool:
    """True when *token* is a PUBLIC identifier rather than a credential shape.

    ``core.secret_patterns.apply_ssot_shapes`` already exempts a public address
    under the bare key ``token`` — and then this filter's LEGACY
    ``token["']?\\s*[:=]`` pattern matched the very same line and masked it
    anyway. The exemption has to be applied on BOTH layers or it is not an
    exemption at all.
    """
    return bool(
        _PUBLIC_ADDRESS_RE.match(token)
        or _TX_HASH_RE.match(token)
        or _UUID_RE.match(token)
    )


def _is_path_shaped(token: str) -> bool:
    """True when *token* is a filesystem path, not a base64 blob.

    The base64 catch-all deliberately KEEPS ``/`` in its character class (a real
    secret can contain one, and round 3 of the A12 fix proved that dropping it
    stops redacting real secrets). Its lookarounds stop a match beginning at an
    ABSOLUTE path's leading ``/`` or mid-path — but a RELATIVE path of 32+
    characters (``data/auto/local/sessions/workspace``) still matched in full,
    so every relative session/workspace path this system logs came out masked.

    The discriminator is structure: a path is many SHORT segments and most of
    them are words. Residual, stated rather than implied: a base64 secret that
    happens to carry two or more internal ``/`` AND two all-alphabetic segments
    survives this net (roughly one in a few hundred). Every named credential
    shape — PEM, Bearer, KV, ``sk-``, ``rob_``, AWS, JWT, opaque-token — runs
    BEFORE the catch-all and is unaffected.
    """
    if token.count("/") < 2:
        return False
    segments = token.split("/")
    if not all(seg and len(seg) <= 24 and seg.isalnum() for seg in segments):
        return False
    return sum(1 for seg in segments if seg.isalpha()) >= 2

# The stock attribute names every logging.LogRecord carries (name, msg, args,
# levelname, pathname, exc_text, threadName, taskName on 3.12+, ...). None of
# these are ever a legitimately "sensitive" field name — only attributes a
# caller adds via ``logger.info(..., extra={...})`` can be. Computed once so
# the SENSITIVE_FIELDS scan below only walks genuine extras instead of every
# standard record attribute on every single record (profiled as the dominant
# remaining per-record cost once the marker pre-check below gates the regex
# battery: iterating ~20 standard attrs through a 19-entry substring scan
# each dwarfed the actual work for a record with no extras at all).
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("_", 0, "_", 0, "", (), None).__dict__.keys()
)

class SecretScrubbingFilter(logging.Filter):
    """Filter that scrubs sensitive information from log records."""

    # Shared high-confidence credential shapes (core/secret_patterns.py SSOT) — the
    # SAME compiled objects core/secret_scrub.py + cli/ui/secrets.py use (identity
    # asserted by test_security_logging_filter.py). Substituted FIRST, each with its
    # own pattern-specific replacement (KV_RE's group(1) is the KEY NAME, not the
    # secret value, so it can't go through the generic group(1)-is-the-secret loop
    # below), via ``_scrub_ssot_shapes``.
    SSOT_PATTERNS = [_PEM_RE, _BEARER_RE, _KV_RE, _PROVIDER_KEY_RE, _POLYROB_KEY_RE, _AWS_RE, _JWT_RE]

    # Legacy hand-rolled patterns — kept for shapes the SSOT doesn't cover (generic
    # api_key=/token=/secret=/password= field patterns, Anthropic/Pinecone-specific
    # keys, and the base64-blob catch-all). Substituted via the generic
    # group(1)-is-the-secret loop in ``scrub_message``.
    LEGACY_PATTERNS = [
        # API keys - various formats
        re.compile(r'(?i)api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=]{20,})["\']?'),
        re.compile(r'(?i)key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=]{20,})["\']?'),
        re.compile(r'(?i)token["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=_-]{20,})["\']?'),
        re.compile(r'(?i)secret["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=_-]{20,})["\']?'),

        # Passwords
        re.compile(r'(?i)password["\']?\s*[:=]\s*["\']?([^\s"\']{6,})["\']?'),
        re.compile(r'(?i)passwd["\']?\s*[:=]\s*["\']?([^\s"\']{6,})["\']?'),

        # Bearer tokens
        re.compile(r'(?i)bearer\s+([a-zA-Z0-9+/=_-]{20,})'),

        # Specific service patterns
        re.compile(r'sk-[a-zA-Z0-9]{48}'),  # OpenAI API keys
        re.compile(r'claude-[a-zA-Z0-9_-]{20,}'),  # Anthropic keys
        re.compile(r'pc-[a-zA-Z0-9]{32}'),  # Pinecone keys

        # Generic base64-looking strings that might be keys. A12 fix round 3
        # (2026-09-14, review finding): the character class KEEPS '/' — a
        # real base64 secret can contain '/' internally (e.g. roughly half
        # of random 40-char tokens do), and no other pattern in this battery
        # backstops a bare token, so dropping '/' from the class (the
        # round-2 attempt) silently stopped redacting real secrets. Only
        # lookarounds were added: the lookbehind refuses a match starting
        # right after '/' or a word character (a path's every INTERNAL
        # segment boundary is one of those two, so a match can never start
        # mid-path), and the lookahead refuses a match ending right before a
        # word character. A token bounded by whitespace/punctuation on both
        # sides — '/' included in its body — still matches in full.
        #
        # A12 fix round 4 (2026-09-14, review finding): round 3's lookbehind
        # alone still let a match START at a path's own leading '/' — that
        # position is preceded by a space (or is the very start of the
        # message), neither of which the lookbehind blocks. A homogeneous
        # absolute path (no digit/underscore/hyphen/period near its start)
        # was still eaten whole from position 0. Added `(?!/)` right after
        # the lookbehind: a match may not itself START with '/'. Since every
        # ABSOLUTE path starts with '/', this closes the gap; a match that's
        # merely blocked from starting AT the '/' can't restart one
        # character later either, because that position is now preceded by
        # '/' — caught by the existing lookbehind. Accepted blind spot: a
        # secret that itself happens to start with '/' (~1/64 chance for a
        # random base64 character) is not redacted by this pattern — no
        # simple lookaround can distinguish "a path" from "a secret that
        # starts with the path-separator byte" from the leading character
        # alone.
        re.compile(r'(?<![/\w])(?!/)([a-zA-Z0-9+/]{32,}={0,2})(?!\w)'),
    ]

    # Back-compat / introspection: the full combined pattern list (SSOT ∪ legacy).
    SECRET_PATTERNS = SSOT_PATTERNS + LEGACY_PATTERNS

    # Cheap pre-check markers (review follow-up, T2.5 same day): most patterns in
    # SECRET_PATTERNS require one of these lowercase substrings to appear somewhere
    # in their match (field-name prefixes like "key=", "token=", "password=", the
    # "Bearer " scheme, the "-----BEGIN" PEM header, or a provider-key prefix like
    # "sk-"/"rob_"/"AKIA"). Three legacy patterns match WITHOUT any marker word —
    # the "claude-"/"pc-" provider prefixes and the bare base64-blob catch-all
    # (the net that covers ghp_/AIzaSy-style tokens) — so the battery may only be
    # skipped when BOTH this marker scan AND _MARKERLESS_PRECHECK miss.
    MARKER_WORDS = (
        "key", "token", "secret", "bearer", "akia", "-----begin",
        "sk-", "pk-", "rk-", "rob_", "password", "authorization",
        # JWTs always start with eyJ (base64 of '{"') — without this marker a
        # bare OAuth token would be gated out of the battery entirely (base64url
        # `-`/`_` chars break the 32-char alnum run in _MARKERLESS_PRECHECK).
        "eyj",
    )

    # Single-scan stand-in for the three marker-less LEGACY_PATTERNS: matches iff
    # one of them COULD match ("claude-"/"pc-" literal prefix, or any ≥32-char
    # base64 run). Keeps the benign fast path (one search, no substitution) while
    # never gating those secret shapes out of the battery.
    _MARKERLESS_PRECHECK = re.compile(r'claude-|pc-|[a-zA-Z0-9+/]{32}')

    # Fields that should always be scrubbed if they contain potential secrets
    SENSITIVE_FIELDS = {
        'api_key', 'apikey', 'api-key',
        'secret_key', 'secretkey', 'secret-key', 'secret',
        'password', 'passwd', 'pass',
        'token', 'access_token', 'refresh_token',
        'authorization', 'auth',
        'key', 'private_key', 'public_key',
        'client_secret', 'client_id'
    }

    def __init__(self, mask_length: int = 4, mask_char: str = '*'):
        """
        Initialize the filter.

        Args:
            mask_length: Number of characters to show at start/end
            mask_char: Character to use for masking
        """
        super().__init__()
        self.mask_length = mask_length
        self.mask_char = mask_char

    def mask_secret(self, secret: str) -> str:
        """Mask a secret string, showing only first/last few characters."""
        if len(secret) <= self.mask_length * 2:
            return self.mask_char * len(secret)

        start = secret[:self.mask_length]
        end = secret[-self.mask_length:]
        middle_length = len(secret) - (self.mask_length * 2)
        return f"{start}{self.mask_char * min(middle_length, 10)}{end}"

    @classmethod
    def _has_marker(cls, text: str) -> bool:
        """True if *text* contains any of ``MARKER_WORDS`` (case-insensitive).

        One ``str.lower()`` + one ``any(... in ...)`` scan — orders of magnitude
        cheaper than running the ~21-pattern regex battery. NOT a sufficient gate
        alone: three legacy patterns match without any marker word, so callers
        must OR this with a ``_MARKERLESS_PRECHECK`` search before skipping the
        battery.
        """
        if not text:
            return False
        low = text.lower()
        return any(marker in low for marker in cls.MARKER_WORDS)

    def _scrub_ssot_shapes(self, text: str) -> str:
        """Redact the shared high-confidence credential shapes via the ONE
        ordered battery in core/secret_patterns.apply_ssot_shapes. This filter
        used to carry its own copy that had silently dropped the JWT rung, so
        OAuth access/refresh tokens survived into log lines."""
        return apply_ssot_shapes(text, _REDACTED)

    def scrub_message(self, message: str) -> str:
        """Scrub secrets from a message string.

        The SSOT battery runs first (it carries the public-address exemption);
        the legacy patterns then run through the SAME exemption gate, because a
        rule that un-redacts on one layer and re-redacts on the next has not
        exempted anything (C28, 2026-09-21).
        """
        scrubbed = self._scrub_ssot_shapes(message)

        # Apply the remaining legacy patterns
        for pattern in self.LEGACY_PATTERNS:
            def replace_secret(match):
                full_match = match.group(0)
                if len(match.groups()) > 0:
                    secret = match.group(1)
                    if _is_public_identifier(secret) or _is_path_shaped(secret):
                        return full_match
                    masked_secret = self.mask_secret(secret)
                    return full_match.replace(secret, masked_secret)
                if _is_public_identifier(full_match) or _is_path_shaped(full_match):
                    return full_match
                return self.mask_secret(full_match)

            scrubbed = pattern.sub(replace_secret, scrubbed)

        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Filter method called on each log record.

        Returns:
            True to allow the record through, False to drop it
        """
        # Per-record dedup (review follow-up, T2.5 same day): the SAME LogRecord
        # is filtered once per Handler it passes through. record.msg/args/exc_text
        # are mutated IN PLACE below, so a record already scrubbed by an earlier
        # handler on this logger is already correct for every later handler — no
        # need to re-run anything (including the marker pre-check itself).
        if getattr(record, '_secret_scrubbed', False):
            return True

        # Cheap pre-check (review follow-up, T2.5 same day): build a candidate
        # string spanning every surface the regex battery below scrubs — msg,
        # args, and exception text — and skip the battery entirely unless it
        # contains a marker. A message with a benign msg but secret-bearing args
        # (or vice versa) must still scrub, hence checking the CONCATENATION
        # rather than gating each surface independently. The exception surface
        # uses the cheap ``str(exc_value)`` rather than a full traceback render
        # (that render itself is deferred into the marker-gated branch below).
        raw_msg = getattr(record, 'msg', None)
        msg_text = raw_msg if isinstance(raw_msg, str) else ''
        args_val = getattr(record, 'args', None)
        args_text = str(args_val) if args_val else ''
        exc_info = getattr(record, 'exc_info', None)
        exc_candidate = ''
        if exc_info:
            try:
                exc_candidate = str(exc_info[1])
            except Exception:
                exc_candidate = ''

        candidate = f"{msg_text}{args_text}{exc_candidate}"
        if self._has_marker(candidate) or self._MARKERLESS_PRECHECK.search(candidate):
            # Scrub the main message
            if msg_text:
                record.msg = self.scrub_message(msg_text)

            # Scrub %-style interpolation args in place — anything downstream that
            # reads record.args directly (a custom formatter, a structured-log
            # exporter) must not see the raw, unscrubbed value either; relying
            # solely on the getMessage() override below only protects consumers
            # that call it.
            if isinstance(args_val, tuple):
                record.args = tuple(
                    self.scrub_message(a) if isinstance(a, str) else a
                    for a in args_val
                )
            elif isinstance(args_val, dict):
                record.args = {
                    k: (self.scrub_message(v) if isinstance(v, str) else v)
                    for k, v in args_val.items()
                }

            # Scrub formatted message
            if hasattr(record, 'getMessage'):
                try:
                    original_get_message = record.getMessage

                    def scrubbed_get_message():
                        msg = original_get_message()
                        return self.scrub_message(msg) if isinstance(msg, str) else msg

                    record.getMessage = scrubbed_get_message
                except Exception:
                    pass

            # Scrub exception text. record.exc_info holds the live exception
            # object — its message (and therefore anything embedded in it, e.g.
            # an API key interpolated into an error string) is rendered lazily by
            # Formatter.formatException() at FORMAT time, after filters have
            # already run, so scrubbing record.msg alone never touches it.
            # Pre-computing a scrubbed record.exc_text here wins: Formatter.format()
            # only computes exc_text "if not record.exc_text already set".
            if exc_info:
                try:
                    raw = ''.join(traceback.format_exception(*exc_info))
                    if raw.endswith('\n'):
                        raw = raw[:-1]
                    record.exc_text = self.scrub_message(raw)
                except Exception:
                    pass

        # Scrub any dictionary-like extra data. Keyed on FIELD NAME, not message
        # content, so it is intentionally NOT gated by the marker pre-check above
        # (e.g. ``logger.info("ok", extra={"password": "..."})`` has a wholly
        # benign msg/args/exc but a sensitive extra field). Restricted to
        # non-standard keys (genuine ``extra=`` fields) — see
        # ``_STANDARD_RECORD_ATTRS`` — since scanning every stock LogRecord
        # attribute against SENSITIVE_FIELDS on every record was pure waste.
        if hasattr(record, '__dict__'):
            for key, value in record.__dict__.items():
                if key in _STANDARD_RECORD_ATTRS:
                    continue
                if isinstance(value, str) and (
                    key.lower() in self.SENSITIVE_FIELDS or
                    any(field in key.lower() for field in self.SENSITIVE_FIELDS)
                ):
                    record.__dict__[key] = self.mask_secret(value)

        record._secret_scrubbed = True
        return True


def install_secret_scrubbing_filter(logger_name: str = None):
    """
    Install the secret scrubbing filter on a logger.

    Args:
        logger_name: Name of logger to install on, None for root logger
    """
    logger = logging.getLogger(logger_name)

    # Check if filter is already installed
    for filter_obj in logger.filters:
        if isinstance(filter_obj, SecretScrubbingFilter):
            return

    filter_obj = SecretScrubbingFilter()
    logger.addFilter(filter_obj)