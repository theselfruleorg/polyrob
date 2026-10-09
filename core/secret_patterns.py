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
#: space and leave the token). 8+ token chars. ``Basic <base64>`` needs a
#: base64-shaped token (a digit, ``+``, ``/`` or an inner capital): plain prose
#: such as "basic features" is not a credential and must survive the scrub.
BEARER_RE = re.compile(
    r"\b(?i:Bearer)\s+[A-Za-z0-9._+/=\-]{8,}"
    r"|\b(?i:Basic)\s+[A-Za-z0-9+/](?=[A-Za-z0-9+/]*[0-9+/A-Z])[A-Za-z0-9+/]{7,}={0,2}"
)

#: ``key = value`` / ``key: value`` where the key NAME signals a credential.
#: (?<![A-Za-z0-9]) + prefix-capture instead of a leading \b so that
#: ``<PREFIX>_API_KEY=`` (the most common real env-var shape) matches — a `\b`
#: fails there because `_` is a word char. Key preserved; value (>=6 chars) redacted.
KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|apikey|secret|client_secret|password|passwd|"
    r"access[_-]?token|auth[_-]?token|token|authorization|bearer|"
    # 2026-09-21 revalidation: the wallet-shaped keys were never claimed, so
    # `private_key=0x…`, `WALLET_PRIVATE_KEY=…`, `mnemonic=…` survived every
    # scrubber verbatim while `secret=…` was caught.
    r"private[_-]?key|privatekey|mnemonic|seed|seed[_-]?phrase|passphrase|keystore|"
    # SUP-8: env names whose credential word is a two-part key name
    # (`MCP_ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `AWS_SECRET_ACCESS_KEY`,
    # `TWITTER_CHAT_PRIVATE_KEYS_B64`) and connection-string DSNs.
    r"encryption[_-]?key|secret[_-]?key|secret[_-]?access[_-]?key|"
    r"private[_-]?keys|signing[_-]?key|master[_-]?key|session[_-]?key|dsn)"
    # An encoding suffix on any of the names above (`…_B64`, `…_HEX`).
    r"(?:[_-](?:b64|base64|hex|pem|json))?)"
    r"(['\"]?\s*[=:]\s*)"
    r"(['\"]?)([^\s'\"]{6,})\3"
)

#: Provider-style opaque keys: ``sk-``/``pk-``/``rk-`` (OpenAI/Anthropic/Stripe).
PROVIDER_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")
GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")
URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^\s/@]+@")
#: SUP-8: ``user:password@`` in ANY scheme (``postgres://``, ``redis://``,
#: ``amqp://``, ``mongodb+srv://`` …). Only the password is redacted; a bare
#: ``ssh://git@host`` user (no colon) is not a credential and is left alone.
URL_PASSWORD_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,20}://[^\s/@:]+:)[^\s/@]+@")
#: SUP-8: Stripe-style live/test secret and restricted keys (``sk_live_…``).
#: ``pk_`` is a publishable key and is left alone.
STRIPE_KEY_RE = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}")
COOKIE_RE = re.compile(r"(?im)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+")

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

#: OPAQUE OAuth tokens (024 L2). Plenty of subscription providers issue tokens
#: that are neither JWT-shaped nor ``sk-``-prefixed — a bare high-entropy blob
#: with a short vendor prefix (``gho_``, ``ghu_``, ``github_pat_``, ``oat_``,
#: ``sbp_``, ``xoxb-``, …). Every rule above matches on SHAPE, so those passed
#: through the whole battery untouched and could land in a log line or a
#: persisted transcript verbatim.
#:
#: Shape: one or two lowercase prefix segments, a separator, then an UNBROKEN
#: run of >=24 alphanumerics (``gho_16C7e…``, ``github_pat_11ABC…``,
#: ``sbp_0102…``). The unbroken run is what keeps ordinary snake_case out: a
#: path or identifier like ``test_export_defaults_into_sess0`` is long enough in
#: total but has no 24-char alphanumeric run, and an earlier, looser version of
#: this rule redacted exactly that out of a pytest tmpdir path.
#:
#: Deliberately NOT a bare high-entropy catch-all: callers that want one already
#: layer hex/base64 catch-alls AFTER this battery, and doing it here would
#: redact identifiers, hashes and file paths across every log in the system.
#: Hyphen-segmented tokens (Slack's ``xoxb-1-2-abc``) are out of scope — they
#: have no long run either, and widening for them costs more than it buys.
OPAQUE_TOKEN_RE = re.compile(
    r"\b[a-z][a-z0-9]{1,15}(?:_[a-z0-9]{1,15})?[_-][A-Za-z0-9]{24,}\b"
)


#: A managed-RPC endpoint whose API KEY IS THE PATH (S10, 2026-09-14).
#:
#: ``https://base-mainnet.g.alchemy.com/v2/<32 chars>`` carries its credential in
#: a path segment, so NO rule above could see it: there is no ``key=value``, no
#: ``sk-`` prefix, no ``Bearer``. The ``?api-key=<…>`` form (Helius, QuickNode)
#: was already covered by ``KV_RE``; the path form was not, and prod's Alchemy
#: URL was returned intact by ``GET /api/webgate/config`` and by the agent's own
#: ``preferences explain``.
#:
#: Only the KEY SEGMENT is redacted — the scheme, host and path prefix survive,
#: so an operator can still read WHICH provider and chain an endpoint points at,
#: and an agent holding a URL in persisted history does not lose the host it was
#: talking to. Redacting the whole URL would trade one blindness for another.
#:
#: Deliberately anchored, because this battery also runs over PERSISTED model
#: content where a false positive destroys working data. Two anchors, checked in
#: :func:`_rpc_key_replacement`:
#:   1. the segment before the key is an API version marker (``/v2/``, ``/v3/``)
#:      — Alchemy and Infura — OR the host is a known managed-RPC provider;
#:   2. the segment LOOKS like a key, not a documentation slug (see
#:      :func:`_looks_like_path_key`). ``/v2/getting-started-with-the-api`` is
#:      long enough to match the regex and must NOT be redacted.
RPC_URL_PATH_KEY_RE = re.compile(
    r"(?i)(https?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[A-Za-z0-9._\-]+)*?/)"
    r"([A-Za-z0-9_\-]{24,})(?![A-Za-z0-9_\-])"
)

#: Hosts that sell authenticated JSON-RPC. On these, a long trailing path
#: segment IS the credential regardless of whether a version marker precedes it
#: (QuickNode: ``https://<name>.quiknode.pro/<40 hex>/``).
_RPC_PROVIDER_HOSTS = (
    "alchemy.com", "alchemyapi.io", "infura.io", "quiknode.pro", "quicknode.com",
    "chainstack.com", "ankr.com", "blastapi.io", "nodereal.io", "getblock.io",
    "blockdaemon.com", "helius-rpc.com", "helius.xyz", "rpcpool.com", "drpc.org",
    "tenderly.co", "moralis.io", "omniatech.io", "blockpi.network", "zan.top",
    "allthatnode.com", "syndica.io", "shyft.to", "nownodes.io", "chainnodes.org",
    "triton.one", "quicknode.pro", "rpc.grove.town", "pokt.network",
)

_VERSION_SEGMENT_RE = re.compile(r"^v\d{1,2}$", re.IGNORECASE)


def _looks_like_path_key(segment: str) -> bool:
    """Whether a >=24-char path segment is a credential rather than a slug.

    A documentation slug is hyphen/underscore-separated WORDS
    (``getting-started-with-the-api``); a key is one high-entropy run. So: a
    segment made only of alphabetic words is never a key, and a key must carry
    at least one digit OR mix letter case.
    """
    parts = [p for p in re.split(r"[-_]", segment) if p]
    if len(parts) > 1 and all(p.isalpha() for p in parts):
        return False
    has_digit = any(c.isdigit() for c in segment)
    has_mixed_case = segment.lower() != segment and segment.upper() != segment
    return has_digit or has_mixed_case


def _rpc_key_replacement(match: "re.Match", redacted: str) -> str:
    prefix, key = match.group(1), match.group(2)
    if not _looks_like_path_key(key):
        return match.group(0)
    host = prefix.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    known_host = any(host == h or host.endswith("." + h) for h in _RPC_PROVIDER_HOSTS)
    prev_segment = prefix.rstrip("/").rsplit("/", 1)[-1]
    if not known_host and not _VERSION_SEGMENT_RE.match(prev_segment):
        return match.group(0)
    return f"{prefix}{redacted}"


#: A PUBLIC blockchain address, which ``KV_RE`` would otherwise eat.
#:
#: ``KV_RE`` claims the bare key name ``token``, and in a crypto tree "token" means
#: a CONTRACT, not a credential. The launch verb's own success line is
#: ``  token: 0x…``; redacting it destroyed the single fact the transaction
#: created, and the agent had to recover its own deployed token's address from a
#: third-party indexer.
#:
#: Deliberately narrow, because this is a hole in a redaction rule:
#:   * EVM: ``0x`` + EXACTLY 40 hex. An EVM private key is ``0x`` + 64 hex and is
#:     NOT matched here — widening to any hex run would exempt private keys.
#:   * Solana: base58 of 43-44 chars, the length a 32-byte pubkey encodes to. The
#:     looser 32-44 range would start to overlap ordinary opaque API tokens.
#: Applied ONLY when the key is the bare word ``token`` — ``access_token``,
#: ``auth_token``, ``bot_token``, ``api_key`` and friends keep redacting an
#: address-shaped value, because none of them ever holds one.
#:
#: The residual, stated rather than implied: a credential that is exactly 43-44
#: base58 characters AND sits under the bare key ``token`` would survive here.
#: The named-shape rules (``sk-``, ``ghp_``-style opaque, JWT) run after KV and
#: catch the common ones; this is the gap that is left, and it was taken
#: deliberately against losing every Solana mint the agent writes down.
PUBLIC_ADDRESS_RE = re.compile(
    r"^(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{43,44})$"
)


#: M13 — shapes the battery used to miss entirely.
#:
#: A Telegram bot token, bare (``123456789:AA…``, 35-char secret) or inside the
#: Bot API URL (``/bot123456789:AA…/sendMessage``), where no key NAME precedes it.
TELEGRAM_BOT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:bot)?\d{5,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"
)

#: A Google API key (``AIza`` + 35), e.g. in a ``?key=AIza…`` URL the KV rule
#: misses when the name is just ``key``.
GOOGLE_API_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")

#: A RAW EVM private key (32 bytes hex, optional ``0x``) — but ONLY right after a
#: key-context label (``Private key: 0x…``, ``signing key = …``). ``0x`` + 64 hex
#: is ALSO a transaction hash, a block hash and a storage slot, which this
#: battery sees constantly in persisted content; redacting every one would
#: destroy the agent's own records. The label is kept, the hex is redacted.
#: ``private_key=…`` (no space) was already KV's.
EVM_KEY_CONTEXT_RE = re.compile(
    r"(?i)(\b(?:private|priv|secret|signing|wallet)[ _-]?key\b"
    r"(?:\s*\([^)\n]{0,16}\))?(?:\s+(?:is|was))?\s*[:=]?\s*[\"'`]?)"
    r"(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])"
)

#: A Solana 64-byte secret key in base58 (86-88 chars). A Solana TRANSACTION
#: SIGNATURE is also 64 bytes of base58, so a run preceded (within 32 chars on
#: its line) by a signature/tx word, or sitting in a URL path, is left alone —
#: see :func:`_solana_secret_replacement`. Residual: a bare list of signatures
#: with no label redacts.
SOLANA_SECRET_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{86,88}"
                              r"(?![1-9A-HJ-NP-Za-km-z])")
_SOLANA_SIG_CONTEXT_RE = re.compile(r"(?i)(?:sig|signature|tx|txid|txn|hash|transaction)\b|/$")


def _solana_secret_replacement(match: "re.Match", redacted: str) -> str:
    before = match.string[max(0, match.start() - 32):match.start()].rsplit("\n", 1)[-1]
    if before.endswith("/") or _SOLANA_SIG_CONTEXT_RE.search(before):
        return match.group(0)
    return redacted


def _kv_replacement(match: "re.Match", redacted: str) -> str:
    """The KV substitution, with the public-address exemption.

    Emits ``key<sep><redacted>`` — the quote group is dropped, exactly as the
    previous inline lambda did; only the exemption is new.
    """
    key, sep, value = match.group(1), match.group(2), match.group(4)
    if key.lower() == "token" and PUBLIC_ADDRESS_RE.match(value):
        return match.group(0)
    return f"{key}{sep}{redacted}"


def apply_ssot_shapes(text: str, redacted: str = REDACTED) -> str:
    """Apply the ordered high-confidence shape battery to *text*.

    ONE home for the substitution sequence (PEM → Bearer → Telegram bot token →
    Google key → context-anchored EVM key → RPC-path-key → KV → provider-key →
    polyrob-key → AWS → JWT → Solana secret → opaque token) so the persisted-content scrubber
    (core/secret_scrub.py), the logging filter (core/security_logging_filter.py)
    and the display scrubber (cli/ui/secrets.py) can never drift again — the
    logging filter had already dropped the JWT rung, so OAuth tokens survived
    the battery in log lines. Order constraints: Bearer must run before KV (the
    kv value stops at the first space), and callers layering catch-alls (hex/
    base64) must apply them AFTER this battery so a JWT redacts as one unit.
    """
    out = PEM_RE.sub(redacted, text)
    out = BEARER_RE.sub(redacted, out)
    out = GITHUB_PAT_RE.sub(redacted, out)
    out = URL_USERINFO_RE.sub(lambda m: m.group(1) + redacted + "@", out)
    out = URL_PASSWORD_RE.sub(lambda m: m.group(1) + redacted + "@", out)
    out = STRIPE_KEY_RE.sub(redacted, out)
    out = COOKIE_RE.sub(redacted, out)
    out = TELEGRAM_BOT_TOKEN_RE.sub(redacted, out)
    out = GOOGLE_API_KEY_RE.sub(redacted, out)
    out = EVM_KEY_CONTEXT_RE.sub(lambda m: f"{m.group(1)}{redacted}", out)
    # BEFORE the KV rule: an `?api-key=` URL is KV's job, but a path-form RPC key
    # must be claimed while the whole URL is still intact (KV would otherwise
    # have eaten the query string of a URL that carries both).
    out = RPC_URL_PATH_KEY_RE.sub(lambda m: _rpc_key_replacement(m, redacted), out)
    out = KV_RE.sub(lambda m: _kv_replacement(m, redacted), out)
    out = PROVIDER_KEY_RE.sub(redacted, out)
    out = POLYROB_KEY_RE.sub(redacted, out)
    out = AWS_RE.sub(redacted, out)
    out = JWT_RE.sub(redacted, out)
    out = SOLANA_SECRET_RE.sub(lambda m: _solana_secret_replacement(m, redacted), out)
    from core.security.secret_material import scrub_wallet_material
    out = scrub_wallet_material(out, redacted)
    # LAST: the opaque-token rule is the loosest of the battery, so every
    # named-shape rule gets first refusal on a match.
    out = OPAQUE_TOKEN_RE.sub(redacted, out)
    return out
