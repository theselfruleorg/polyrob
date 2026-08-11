"""Shared high-confidence credential-shape regexes (P4 finalization).

These six patterns were defined IDENTICALLY (byte-for-byte) in both
``core/secret_scrub.py`` (persisted-content scrub, conservative subset) and
``cli/ui/secrets.py`` (display scrub, full set). They drifted once already — the
``KV_RE`` fix for ``<PREFIX>_API_KEY=`` was made in the CLI twin but not backported
to core, silently leaking that shape from persisted message history. Defining them
in ONE place makes that class of divergence impossible: both scrubbers import from
here and layer their own extra patterns on top.

Pure module — regex objects only, no I/O.
"""
import re

#: Replacement marker (the ``<secret>…</secret>`` shape the history filter uses).
REDACTED = "<secret>redacted</secret>"

#: PEM private-key blocks (multi-line) — redact the whole block.
PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: ``Bearer <token>`` — run before the kv rule (the kv value would stop at the
#: space and leave the token). 8+ token chars.
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")

#: ``key = value`` / ``key: value`` where the key NAME signals a credential.
#: (?<![A-Za-z0-9]) + prefix-capture instead of a leading \b so that
#: ``<PREFIX>_API_KEY=`` (the most common real env-var shape) matches — a `\b`
#: fails there because `_` is a word char. Key preserved; value (>=6 chars) redacted.
KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|apikey|secret|client_secret|password|passwd|"
    r"access[_-]?token|auth[_-]?token|token|authorization|bearer))"
    r"(\s*[=:]\s*)"
    r"(['\"]?)([^\s'\"]{6,})\3"
)

#: Provider-style opaque keys: ``sk-``/``pk-``/``rk-`` (OpenAI/Anthropic/Stripe).
PROVIDER_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")

#: POLYROB API keys (``rob_…``).
POLYROB_KEY_RE = re.compile(r"\brob_[A-Za-z0-9]{16,}")

#: AWS access-key id (``AKIA…``).
AWS_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

#: JWTs (``eyJ<header>.<payload>.<sig>``) — OAuth access/refresh tokens from
#: subscription providers (Nous, OpenAI OAuth, …) are commonly JWT-shaped but
#: carry no ``sk-``-style prefix, so none of the rules above matched them by
#: name (proposal 024 §7.2 — previously only length-matched by the CLI's
#: incidental base64 catch-all, and not at all by the persisted-content scrub).
#: The signature segment may be empty (alg=none), hence ``{0,}`` at the tail.
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")


def apply_ssot_shapes(text: str, redacted: str = REDACTED) -> str:
    """Apply the ordered high-confidence shape battery to *text*.

    ONE home for the substitution sequence (PEM → Bearer → KV → provider-key →
    polyrob-key → AWS → JWT) so the persisted-content scrubber
    (core/secret_scrub.py), the logging filter (core/security_logging_filter.py)
    and the display scrubber (cli/ui/secrets.py) can never drift again — the
    logging filter had already dropped the JWT rung, so OAuth tokens survived
    the battery in log lines. Order constraints: Bearer must run before KV (the
    kv value stops at the first space), and callers layering catch-alls (hex/
    base64) must apply them AFTER this battery so a JWT redacts as one unit.
    """
    out = PEM_RE.sub(redacted, text)
    out = BEARER_RE.sub(redacted, out)
    out = KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{redacted}", out)
    out = PROVIDER_KEY_RE.sub(redacted, out)
    out = POLYROB_KEY_RE.sub(redacted, out)
    out = AWS_RE.sub(redacted, out)
    out = JWT_RE.sub(redacted, out)
    return out
