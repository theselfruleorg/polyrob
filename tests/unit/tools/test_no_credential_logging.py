"""No module may log a full credential.

H5 (audit 2026-08-22): tools/collabland/collabland_tool.py carried
`self.logger.debug(f"Full key for debugging: {self._api_key}")  # Temporary debug
- remove in production`. It was never removed. This test is the ratchet.

COVERAGE LIMIT (honest, not exhaustive; DEFERRED by the 2026-08-22 fix-round-1
review — do not silently assume these are covered): this ratchet only
catches a credential interpolated directly into an f-string (`ast.JoinedStr`)
passed as a positional arg to a logger call. It does NOT catch:
  - %-style logging (`logger.debug("key: %s", self._api_key)`) — there is no
    `{identifier}` for the detector to inspect; the value never touches a
    brace.
  - `.format()`-style interpolation (`logger.debug("key: {}".format(key))`)
    — the logger's arg is an `ast.Call` (the `.format()` call), not a
    `JoinedStr`, so the AST walk skips it before the detector ever runs.
  - A credential baked into a variable *before* the logging call
    (`msg = f"...{key}..."; logger.debug(msg)`) — the logger's arg is a
    `Name`, not a `JoinedStr`.
Only the f-string path is covered because that is what the real H5 bug used
and it is overwhelmingly the dominant style in this codebase; the other two
styles are a known gap for a follow-up ratchet, confirmed 0 live instances
tree-wide at review time.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# Detector history (read this before touching the rules below):
#
# R7 (original plan): whole-identifier regex matching any name CONTAINING
# "token"/"seed" etc. — 60 false positives tree-wide (max_tokens, token_id,
# total_tokens, token_symbol, ...).
#
# R13 (fix round 1, task-7 brief): whole-string match against a fixed word
# list, "generic word (key/token/seed) triggers whenever the identifier has
# more than one part." Fixed R7's false positives but over-corrected into
# false NEGATIVES (`self._secret_key`, `self.wallet_private_key`,
# `self.hmac_secret`, `self.db_password`, `self.oauth_secret`,
# `self._api_secret`, `self.aws_secret_access_key`, `self.signing_key`,
# `self._encryption_key` all MISSED) — component-wise fixed the false
# negatives, but "any qualifier at all" was itself backwards: it produced
# 18 NEW false positives tree-wide (`_KEY_FILE_PATH`, `key_prefix`,
# `oldest_key`, `token_id_str`, `token_info`, `session_key`, `prompt_key`,
# `is_jwt_token`).
#
# R14 (fix round 2, this version): three classes replace the single
# "generic + any qualifier" rule:
#   A. a boolean predicate (`is_*`/`has_*`/`was_*`/`should_*`/`can_*`) never
#      holds the credential itself — never trigger, no matter what else
#      matches.
#   B. derived metadata ABOUT a credential (its file path, a deliberate
#      truncation, a byte count, a lookup id, a usage-accounting string...)
#      is not the credential's value — never trigger, no matter what else
#      matches.
#   C. `key`/`token`/`seed` trigger only when qualified by a word from
#      THAT word's own credential-flavoured allow-list — not by any
#      qualifier at all (this was R13's bug).
# STRONG words (secret/password/passwd/credential(s)/mnemonic/privkey/jwt/
# apikey) still trigger unconditionally, subject to Class A/B suppression.
#
# No hand-maintained safe-compound list is needed any more: every one of
# R13's 31 original safe entries AND the 18 round-1 false positives were
# verified (by hand, against this exact rule set) to already be excluded
# by Classes A/B/C alone — adding a per-identifier allowlist on top would
# just rebuild the hand-maintained-list problem this ratchet exists to
# remove.
# ---------------------------------------------------------------------------

_STRONG_COMPONENTS = {
    "secret", "password", "passwd", "credential", "credentials",
    "mnemonic", "privkey", "jwt", "apikey",
}

#: R14 Class A — a boolean predicate holds a bool, never the credential.
_BOOLEAN_PREFIXES = ("is_", "has_", "was_", "should_", "can_")

#: R14 Class B — derived metadata about a credential is not the credential.
_METADATA_SUFFIXES = (
    "_path", "_file", "_prefix", "_suffix", "_len", "_length", "_count",
    "_id", "_ids", "_name", "_type", "_info", "_uri", "_url", "_hash",
    "_index", "_usage", "_str", "_format", "_scheme",
)

#: R14 Class C — per-generic-word qualifier allow-lists. `key`/`token`/
#: `seed` trigger ONLY when co-present (anywhere in the identifier, as a
#: contiguous run — see `_has_qualifier`) with one of their own qualifiers.
#:
#: NOTE: "session_secret" is listed here for `key` alongside plain
#: "secret" — they overlap in effect (a genuine `session_secret_key`
#: already triggers via the STRONG "secret" component before Class C is
#: even reached), but both are kept explicit so the next reader can see
#: `session_secret_key` is deliberately still caught, and is NOT
#: accidentally excluded by the same Class C rewrite that lets the
#: unrelated `session_key` (a routing/lookup key, no "secret" in it) pass.
_KEY_QUALIFIERS = {
    "api", "secret", "private", "priv", "signing", "sign", "encryption",
    "encrypt", "access", "auth", "master", "session_secret", "hmac",
    "client", "app", "shared", "symmetric",
}
_TOKEN_QUALIFIERS = {
    "api", "auth", "access", "refresh", "bearer", "bot", "session",
    "gateway", "jwt", "id_token", "client", "oauth", "personal", "pat",
}
_SEED_QUALIFIERS = {
    "master", "wallet", "mnemonic", "recovery", "entropy", "hd",
}

#: Matches an f-string interpolation whose ENTIRE content — nothing but
#: optional whitespace, an optional `self.` prefix, an optional leading
#: underscore, and a bare identifier — is a single name/attribute access.
#: Deliberately does NOT match a slice (`{self._api_key[:5]}`), a call
#: (`{len(x)}`), or a format spec (`{x:.2f}`): slicing to a prefix is
#: exactly the fix this ratchet asks for, and those other shapes are out of
#: scope (see the COVERAGE LIMIT note above).
_INTERP = re.compile(r"\{\s*((?:self\.)?_?[A-Za-z][A-Za-z0-9_]*)\s*\}")


def _normalize_identifier(raw: str) -> str:
    ident = raw.strip()
    if ident.startswith("self."):
        ident = ident[len("self."):]
    ident = ident.lstrip("_")
    return ident.lower()


def _has_qualifier(parts, qualifiers) -> bool:
    """True if any (possibly multi-word, `_`-joined) qualifier in
    `qualifiers` appears as a contiguous run within `parts`.
    """
    for q in qualifiers:
        q_parts = q.split("_")
        n = len(q_parts)
        if any(parts[i:i + n] == q_parts for i in range(len(parts) - n + 1)):
            return True
    return False


def is_dangerous_identifier(raw: str) -> bool:
    """True if `raw` (an f-string interpolation's bare identifier, e.g.
    `"self._api_key"`) names a credential, per CONTROLLER RULING R14's
    three-class rule (see the module-level history comment above).
    """
    ident = _normalize_identifier(raw)
    if not ident:
        return False
    parts = ident.split("_")

    # Class A: a boolean predicate never holds the credential itself.
    if any(ident.startswith(prefix) for prefix in _BOOLEAN_PREFIXES):
        return False
    # Class B: derived metadata about a credential is not the credential.
    if any(ident.endswith(suffix) for suffix in _METADATA_SUFFIXES):
        return False

    # STRONG components always trigger (after Class A/B suppression).
    if any(p in _STRONG_COMPONENTS for p in parts):
        return True

    # Class C: key/token/seed trigger only when qualified by a
    # credential-flavoured word from their OWN allow-list.
    if "key" in parts and _has_qualifier(parts, _KEY_QUALIFIERS):
        return True
    if "token" in parts and _has_qualifier(parts, _TOKEN_QUALIFIERS):
        return True
    if "seed" in parts and _has_qualifier(parts, _SEED_QUALIFIERS):
        return True
    return False


# Fix-round-1 review, Important 2: the original scan omitted six production
# directories (webview/, migrations/, scripts/, deployment/, cron/, utils/)
# — 0 hits in all of them at review time (latent, not live), but the test's
# docstring claims "No module may log a full credential" tree-wide, so the
# scan now actually looks tree-wide. `deployment/` currently contains zero
# `.py` files (shell scripts, systemd units, nginx configs) — kept in the
# list deliberately, forward-looking: if a deployment automation script
# ever gets added there, it starts covered rather than silently exempt.
# No directory is excluded: nothing here is vendored or generated code.
_SCAN_DIRS = (
    "tools", "core", "modules", "agents", "api", "surfaces", "cli",
    "webview", "migrations", "scripts", "deployment", "cron", "utils",
)


def _logging_fstrings(path: Path):
    """Yield (lineno, source) for every f-string passed to a logger call.

    Uses `ast.get_source_segment` over the WHOLE call, not just the line at
    `node.lineno`. For a multi-line call:

        self.logger.warning(
            f"...{self._api_key}..."
        )

    `node.lineno` is the call's opening line (`self.logger.warning(`), which
    does not contain the interpolation — a plain `src[node.lineno - 1]`
    lookup would silently miss this. 332 multi-line logger calls with an
    f-string arg were measured tree-wide (2026-08-22); none currently
    interpolate a credential on a continuation line, but the single-line
    lookup would have missed one that did, so the full-segment lookup is
    used unconditionally rather than only when a line-count check trips.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else None
        if name not in ("debug", "info", "warning", "error", "critical", "exception"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                segment = ast.get_source_segment(source, node) or ""
                yield node.lineno, segment


def _find_offenders():
    offenders = []
    for d in _SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            for lineno, segment in _logging_fstrings(path):
                for match in _INTERP.finditer(segment):
                    if is_dangerous_identifier(match.group(1)):
                        flat = " ".join(segment.split())
                        offenders.append(f"{path.relative_to(REPO)}:{lineno}: {flat}")
                        break  # one report per logging call is enough
    return offenders


def test_no_module_logs_a_full_credential():
    offenders = _find_offenders()
    assert not offenders, (
        "these log a whole credential — slice it (`key[:5]}***`) or drop the line:\n"
        + "\n".join(offenders))


# ---------------------------------------------------------------------------
# Detector pin: this matrix IS the specification for `is_dangerous_
# identifier` (fix-round-2 ruling R14) — it matters more than the rule code
# itself. Covers both the fix-round-1 (R13) matrix and the fix-round-2
# (R14) must-catch / must-not-catch lists in full, parametrized, both
# directions.
# ---------------------------------------------------------------------------

_DANGEROUS_IDENTIFIERS = [
    # R13 misses (fix round 1) — the false negatives that motivated the
    # component-wise rewrite in the first place.
    "self._secret_key",             # the R13-named worst case
    "self.wallet_private_key",
    "self.hmac_secret",
    "self.db_password",
    "self.oauth_secret",
    "self._api_secret",
    "self.aws_secret_access_key",
    "self.signing_key",
    "self._encryption_key",
    "self._api_key",                # the real H5 bug's identifier
    "self.client_secret",
    "self.master_seed",
    "self.bearer_token",
    "self.mnemonic",
    "self._apikey",
    # R14 must-catch list (fix round 2) — proves the qualifier allow-lists
    # didn't over-correct back into R13's original false-negative bug.
    "master_seed",
    "refresh_token",
    "bot_token",
    "password",
    "api_key",
    "session_token",
    "gateway_token",
    "access_token",
]

_BENIGN_IDENTIFIERS = [
    # R13 pins (fix round 1).
    "max_tokens",
    "token_id",
    "total_tokens",
    "token_symbol",
    "primary_key",
    "public_key",
    "cache_key",
    "token_type",
    "key",
    "seed",
    # R14 must-not-catch list (fix round 2) — the 18 tree-wide hits R13
    # produced, all confirmed false positives on source inspection, now
    # excluded by Classes A/B/C rather than a hand-maintained safe list.
    "tokens",
    "n_tokens",
    "prompt_tokens",
    "token_count",
    "idempotency_key",
    "sort_key",
    "oldest_key",
    "prompt_key",
    "session_key",
    "key_prefix",
    "token_id_str",
    "token_info",
    "is_jwt_token",
    "_KEY_FILE_PATH",
]

assert len(_DANGEROUS_IDENTIFIERS) == 23
assert len(_BENIGN_IDENTIFIERS) == 24


@pytest.mark.parametrize("identifier", _DANGEROUS_IDENTIFIERS)
def test_detector_catches_dangerous_identifiers(identifier):
    assert is_dangerous_identifier(identifier), (
        f"{identifier!r} should be flagged as a credential-shaped identifier "
        "but was not — the component-wise detector regressed")


@pytest.mark.parametrize("identifier", _BENIGN_IDENTIFIERS)
def test_detector_excludes_benign_identifiers(identifier):
    assert not is_dangerous_identifier(identifier), (
        f"{identifier!r} should NOT be flagged (known-benign) but was — "
        "the component-wise detector regressed")


# ---------------------------------------------------------------------------
# Positive control (fix-round-1 review, Important 3): if `is_dangerous_
# identifier`, `_INTERP`, or the AST walk in `_logging_fstrings` were ever
# silently broken by a refactor, `test_no_module_logs_a_full_credential`
# would pass vacuously forever — the exact failure mode this ratchet exists
# to prevent, one level up. This proves the detection pipeline actually
# FIRES on a known-bad synthetic input written to a throwaway temp file, so
# it does NOT depend on any real repo file staying broken.
# ---------------------------------------------------------------------------

def test_positive_control_detects_a_synthetic_credential_leak(tmp_path):
    bad_file = tmp_path / "synthetic_offender.py"
    bad_file.write_text(
        "class X:\n"
        "    def f(self):\n"
        '        self.logger.debug(f"key: {self._api_key}")\n',
        encoding="utf-8",
    )
    hits = [
        (lineno, segment)
        for lineno, segment in _logging_fstrings(bad_file)
        for match in _INTERP.finditer(segment)
        if is_dangerous_identifier(match.group(1))
    ]
    assert hits, (
        "the detection pipeline (AST walk + _INTERP + is_dangerous_identifier) "
        "failed to catch a KNOWN-BAD synthetic credential leak — the ratchet "
        "itself is broken and would pass vacuously")
