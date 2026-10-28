"""Which off-chain signatures the agent's wallet may produce (proposal 090).

A signature is not a transaction: nothing simulates it, and whoever holds it can
submit it later. So the wallet never signs bytes because someone asked. Every
request is CLASSIFIED first, and the class decides the gate:

* **A — login / proof of wallet.** Plain text bound to the site it is for: an
  EIP-4361 Sign-In With Ethereum message, a Sign-In With Solana message, or a
  service's challenge text. Signed on a legitimate turn (``tools/wallet_sign``).
* **B — value-moving typed data** (orders, EIP-3009, Permit2 transfers). Not
  signable here yet (090 P2); the pinned x402 path keeps its own op.
* **C — authority** (permits, allowances, agent approvals, delegations, and a
  text that does not name the site it is for). Needs the owner (090 P1).
* **D — blind.** A digest or opaque blob, control bytes, a malformed sign-in
  message, typed data before P1. Refused, always.

The SAME function runs in the agent process and in ``polyrob-signer``
(``sign.classified``), which re-classifies the wire payload and refuses a class
it does not compute itself — a compromised agent cannot relabel a request.

The digest is always recomputed from the parsed text; a caller never supplies
one. Pure: no I/O, no clock unless the context omits ``now``.

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional, Tuple
from urllib.parse import urlsplit

A, B, C, D = "A", "B", "C", "D"
SCHEMES = frozenset({"eip191", "solana_message", "eip712"})

#: A login text longer than this is not a login.
MAX_TEXT_BYTES = 4096
#: SIWE/SIWS: the message must expire, and not later than this.
MAX_SIGN_IN_TTL_SEC = 24 * 3600
#: SIWE/SIWS ``issued-at`` may run this far ahead of our clock.
CLOCK_SKEW_SEC = 300
#: A sign-in message with no ``Expiration Time`` (the ``siwe`` library default)
#: signs only when its ``Issued At`` is this fresh: the site just made it.
MAX_OPEN_SIGN_IN_AGE_SEC = 600
_NONCE_RE = re.compile(r"^[A-Za-z0-9]{8,}$")
_HEX_BLOB_RE = re.compile(r"^(0x)?[0-9a-fA-F]{32,}$")
#: One token, no whitespace, this long: base64/base58/hex of something binary.
_OPAQUE_MIN = 64
_ALLOWED_CONTROL = {"\n", "\r", "\t"}
#: EIP-4361 header. The domain is a plain host[:port] — no userinfo, path or
#: backslash, which different URL parsers read as different hosts.
_SIGN_IN_RE = re.compile(
    r"^(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://)?(?P<domain>[A-Za-z0-9.-]+(?::[0-9]{1,5})?) "
    r"wants you to sign in with your (?P<family>Ethereum|Solana) account:$")
_FIELD_RE = re.compile(r"^(?P<key>[A-Z][A-Za-z ]+): (?P<value>.*)$")
#: EIP-4361 field order. Every line after the statement is one of these, in this
#: order, at most once (``Resources:`` is followed by ``- <uri>`` lines).
_FIELD_ORDER = ("uri", "version", "chain id", "nonce", "issued at", "expiration time",
                "not before", "request id", "resources")
#: Texts that derive a key or open an account elsewhere. Signing one hands out
#: a key that controls funds (zkSync Lite, StarkEx/Immutable X, Loopring, dYdX,
#: XMTP …), whoever asks. Never signed.
_KEY_DERIVATION_RE = re.compile(
    r"only sign this message for a trusted client|access zksync account|stark\s*key|starkex|"
    r"starknet|loopring|\bxmtp\b|\bdydx\b|immutable\s*x|\bimx\b|key\s*derivation|"
    r"derive (?:a|an|the|your) (?:\w+ )?key|generate (?:a|an|the|your) (?:\w+ )?key|"
    r"private key|seed phrase|recovery phrase|mnemonic|\bl2 key\b|trading key|signing key",
    re.IGNORECASE)
#: Words that grant something. A login proves who you are; a text that
#: authorizes, delegates, permits or spends is authority (class C). "Approve"
#: is not on the list: wallets' own sign-in texts say "click Sign or Approve".
_AUTHORITY_RE = re.compile(
    r"\b(?:authori[sz](?:e|es|ed|ing|ation)|delegat\w*|permit\w*|"
    r"allowance|spend\w*|transfer\w*|withdraw\w*|grant\w*|power of attorney|"
    r"on (?:my|your) behalf)\b", re.IGNORECASE)
_NEGATION = (r"(?:does\s+not|do\s+not|did\s+not|doesn['’]t|don['’]t|didn['’]t|will\s+not|"
             r"won['’]t|never|cannot|can\s+not|can['’]t|is\s+not|isn['’]t|are\s+not|"
             r"aren['’]t|shall\s+not|must\s+not|nor|without)")
#: Where a negated clause ends: punctuation, or a word that turns the sentence.
_CLAUSE_END = (r"(?=$|[.,;:!?\n]|\s(?:but|however|except|unless|instead|and|although|"
               r"though|while|yet|so|then)\b)")
#: Verbs a negation may govern in a disclaimer: the grants themselves and what a
#: wallet text says it does NOT do ("does not trigger a transaction or spend funds",
#: "will not give anyone access to transfer"). A verb outside the list keeps the
#: clause ("do not hesitate to transfer" is a grant).
_DISCLAIM_VERBS = (r"authori[sz]\w*|delegat\w*|permit\w*|spend\w*|transfer\w*|withdraw\w*|"
                   r"grant\w*|trigger\w*|send\w*|mov(?:e|es|ed|ing)|cost\w*|giv(?:e|es|ing)|"
                   r"allow\w*|initiat\w*|involv\w*|requir\w*|submit\w*|execut\w*|"
                   r"perform\w*|charg\w*|incur\w*|creat\w*|access\w*|approv\w*|"
                   r"cost|result\s+in|lead\s+to")
#: Verbs a negation may govern before a key word: "we will never ASK for your
#: private key" is a warning, not a key derivation. "derive"/"generate" are not
#: here: a text that says it does not derive a key still names one.
_DISCLOSE_VERBS = (r"ask\w*|request\w*|shar(?:e|es|ed|ing)|reveal\w*|stor(?:e|es|ed|ing)|"
                   r"see|need\w*|know\w*|send\w*|giv(?:e|es|ing)|enter\w*|typ(?:e|es|ing)|"
                   r"collect\w*|hold\w*|access\w*|have\s+access")


def _negated_clause_re(verbs: str) -> "re.Pattern":
    return re.compile(rf"\b{_NEGATION}\s+(?:(?:be|to|ever|in\s+any\s+way|any\s+way)\s+)*"
                      rf"(?:{verbs})\b.*?{_CLAUSE_END}", re.IGNORECASE)


#: A negated grant ("It does not authorize a payment" — 402Mob's own challenge;
#: "does not authorize any transfer") says the opposite of a grant: the whole
#: clause the negation governs is removed before ``_AUTHORITY_RE`` looks. A real
#: grant elsewhere (after "but", or in the next sentence) still matches.
_NEGATED_AUTHORITY_RE = _negated_clause_re(_DISCLAIM_VERBS)
#: "no funds will be transferred" — the passive form of the same disclaimer.
_NO_PASSIVE_RE = re.compile(
    rf"\bno\s+(?:[\w-]+\s+){{0,3}}(?:will|can|shall|is|are|may)\s+(?:ever\s+)?be\s+"
    rf"(?:{_DISCLAIM_VERBS})\b.*?{_CLAUSE_END}", re.IGNORECASE)
_NEGATED_DISCLOSE_RE = _negated_clause_re(_DISCLOSE_VERBS)


def _grants(text: str) -> bool:
    text = _NO_PASSIVE_RE.sub(" ", _NEGATED_AUTHORITY_RE.sub(" ", text))
    return bool(_AUTHORITY_RE.search(text))


def _derives_key(text: str) -> bool:
    return bool(_KEY_DERIVATION_RE.search(_NEGATED_DISCLOSE_RE.sub(" ", text)))


#: A host named in a text: any URL host, or a bare name on a common suffix.
_URL_HOST_RE = re.compile(r"\b(?:https?|wss?)://([a-z0-9.-]+)", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"(?<![a-z0-9@.-])((?:[a-z0-9-]+\.)+(?:com|org|net|io|xyz|app|dev|ai|co|fi|finance|"
    r"exchange|gg|me|so|eth|sol|tech|network|money|market|trade|info|biz|us|uk|de|fr|ch|"
    r"jp|top|site|online|cloud|link|to|tv|ly|sh|zone|world|art|nft|dao|wallet|pro|page|"
    r"global|club|fun|bot|gold|cash|capital|games|social|chat|land|one|run|build))"
    r"(?![a-z0-9-])", re.IGNORECASE)
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass(frozen=True)
class SignRequest:
    """What the agent asks to sign. ``payload`` is the exact text for the two
    text schemes (P0)."""

    scheme: str
    payload: Any
    #: The host the agent is talking to. For the fetch-bound form the tool sets
    #: it from the URL it fetched; for the text form the caller names it.
    bound_origin: str = ""
    #: The text came back from an HTTPS request the tool itself made to
    #: ``bound_origin`` — the origin is proven, so the text need not repeat it.
    fetch_bound: bool = False

    def to_wire(self) -> Dict[str, Any]:
        return {"scheme": self.scheme, "payload": self.payload,
                "bound_origin": self.bound_origin, "fetch_bound": bool(self.fetch_bound)}

    @classmethod
    def from_wire(cls, body: Dict[str, Any]) -> "SignRequest":
        return cls(scheme=str(body.get("scheme") or ""), payload=body.get("payload"),
                   bound_origin=str(body.get("bound_origin") or ""),
                   fetch_bound=bool(body.get("fetch_bound")))


@dataclass(frozen=True)
class SignContext:
    #: The address that will sign (EVM checksum or base58).
    holder: str
    now: Optional[float] = None
    #: EIP-155 ids a SIWE ``Chain ID`` may name. None = every pinned EVM chain.
    chain_ids: Optional[FrozenSet[int]] = None
    #: Whether ``SignRequest.fetch_bound`` can be believed. The agent process
    #: made the fetch itself; ``polyrob-signer`` cannot prove it and passes False.
    trust_fetch_bound: bool = True


@dataclass(frozen=True)
class SignVerdict:
    cls: str
    reason: str
    scheme: str = ""
    #: What a person reads: kind, domain, chain, expiry.
    decoded: Dict[str, Any] = field(default_factory=dict)
    #: EIP-191 hash (EVM) or sha256 (Solana) of the exact bytes, hex.
    digest: str = ""
    #: ``(domain, nonce)`` for a sign-in message: one signature per nonce.
    replay_key: Optional[Tuple[str, str]] = None
    #: Class A only on a genuine owner turn: a challenge the tool fetched that
    #: does not name its site. An autonomous run never signs it.
    owner_turn_only: bool = False

    @property
    def signable(self) -> bool:
        return self.cls == A


def _d(reason: str, scheme: str = "", **decoded) -> SignVerdict:
    return SignVerdict(D, reason, scheme, dict(decoded))


def origin_host(origin: str) -> str:
    """The lower-case host of *origin* (a bare host, ``host:port`` or a URL);
    ``""`` when there is none."""
    text = str(origin or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".")


#: Second-level labels under a two-letter country code (``co.uk``, ``com.au``).
_CC_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne",
                              "go", "ltd", "plc"})
#: Hosting suffixes where every subdomain belongs to someone else: on these the
#: site is one label deeper (``a.github.io`` and ``b.github.io`` are two sites).
_SHARED_SUFFIXES = (
    "github.io", "gitlab.io", "vercel.app", "netlify.app", "pages.dev", "workers.dev",
    "herokuapp.com", "web.app", "firebaseapp.com", "appspot.com", "cloudfront.net",
    "azurewebsites.net", "onrender.com", "fly.dev", "replit.app", "repl.co", "glitch.me",
    "ngrok.io", "ngrok.app", "ngrok-free.app", "trycloudflare.com", "amazonaws.com",
    "blogspot.com", "wordpress.com", "substack.com", "myshopify.com", "surge.sh",
    "railway.app", "deno.dev", "netlify.com", "now.sh", "framer.app", "webflow.io",
    "carrd.co", "notion.site", "bubbleapps.io", "mypinata.cloud", "ipfs.io", "eth.limo",
    "eth.link", "dweb.link", "translate.goog")


def site_of(host: str) -> str:
    """The registrable domain of *host*: ``api.example.com`` → ``example.com``,
    ``a.example.co.uk`` → ``example.co.uk``, ``a.github.io`` → ``a.github.io``.
    An IP address or a one-label host is its own site."""
    host = str(host or "").lower().rstrip(".")
    if not host or ":" in host or re.fullmatch(r"[0-9.]+", host):
        return host
    labels = host.split(".")
    for suffix in _SHARED_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return ".".join(labels[-(suffix.count(".") + 2):])
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    """True when hosts *a* and *b* are one site (one registrable domain):
    ``api.example.com`` and ``app.example.com`` are; ``example.com.evil.io`` is not."""
    a, b = origin_host(a), origin_host(b)
    return bool(a) and bool(b) and site_of(a) == site_of(b)


def names_host(text: str, host: str) -> bool:
    """True when *text* names *host* as a whole host: ``api.example.com`` names
    ``example.com``; ``notexample.com`` and ``example.com.evil.io`` do not."""
    if not host:
        return False
    pattern = r"(?<![a-z0-9-])" + re.escape(host) + r"(?![a-z0-9-]|\.[a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def _default_chain_ids() -> FrozenSet[int]:
    from core.wallet.chains import evm_rows
    return frozenset(int(r.chain_id) for r in evm_rows() if r.chain_id)


def _digest(scheme: str, data: bytes) -> str:
    if scheme == "solana_message":
        return hashlib.sha256(data).hexdigest()
    from eth_account.messages import defunct_hash_message
    return "0x" + defunct_hash_message(primitive=data).hex().removeprefix("0x")


def _rlp_list_spans_all(data: bytes) -> bool:
    """True when *data* is exactly one RLP list (the body of a legacy or typed
    EVM transaction). Printable text almost never is; a raw tx always is."""
    if not data:
        return False
    first = data[0]
    if first in (0x01, 0x02, 0x03, 0x04) and len(data) > 1:  # EIP-2718 typed envelope
        data = data[1:]
        first = data[0]
    if first < 0xC0:
        return False
    if first <= 0xF7:
        return 1 + (first - 0xC0) == len(data)
    width = first - 0xF7
    if len(data) < 1 + width:
        return False
    length = int.from_bytes(data[1:1 + width], "big")
    return 1 + width + length == len(data)


def _blind_text_reason(text: str, data: bytes) -> Optional[str]:
    """Why *text* is not a human-readable message, or None."""
    if not text.strip():
        return "the message is empty"
    if len(data) > MAX_TEXT_BYTES:
        return f"the message is {len(data)} bytes; a login text is at most {MAX_TEXT_BYTES}"
    if "\r" in text.replace("\r\n", "") or any(c in text for c in "\u2028\u2029"):
        return ("the message carries a line break other than \\n or \\r\\n — a parser "
                "on the site could split its lines differently")
    for ch in text:
        if ch in _ALLOWED_CONTROL:
            continue
        if ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0:
            return "the message carries control bytes (a binary payload, not text)"
    stripped = text.strip()
    if _HEX_BLOB_RE.match(stripped):
        return ("the message is a hex string — a hash or an encoded transaction, which "
                "signs something nobody can read")
    if len(stripped) >= _OPAQUE_MIN and not any(c.isspace() for c in stripped):
        return ("the message is one opaque encoded token (base64/base58/hex) — a "
                "serialized payload, not text")
    if _rlp_list_spans_all(data):
        return "the message decodes as an RLP-encoded EVM transaction"
    return None


def _parse_time(value: str) -> Optional[float]:
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_field(line: str) -> bool:
    fm = _FIELD_RE.match(line)
    return bool(fm) and fm.group("key").strip().lower() in _FIELD_ORDER


def _parse_sign_in(text: str) -> Optional[Dict[str, Any]]:
    """EIP-4361 / SIWS, parsed strictly, or None when line 1 is not a sign-in
    header. Raises ``ValueError`` for a header followed by a body that a strict
    EIP-4361 parser would read differently (a field in the statement, a field
    out of order or twice, an unknown line)."""
    lines = text.replace("\r\n", "\n").split("\n")
    m = _SIGN_IN_RE.match(lines[0])
    if not m:
        return None
    if len(lines) < 4 or lines[2] != "":
        raise ValueError("the sign-in message has no address line followed by a blank line")
    out: Dict[str, Any] = {"domain": m.group("domain").lower(),
                           "family": m.group("family").lower(),
                           "address": lines[1], "statement": ""}
    i = 3
    if lines[i] == "":                       # EIP-4361 with no statement: two blank lines
        i += 1
    elif not _is_field(lines[i]):            # the statement, then a blank line
        if i + 1 >= len(lines) or lines[i + 1] != "":
            raise ValueError("the sign-in statement must be one line followed by a blank line")
        out["statement"] = lines[i]
        i += 2
    fields: Dict[str, str] = {}
    resources = []
    last = -1
    for line in lines[i:]:
        if line == "" and not fields:
            continue
        if "resources" in fields and line.startswith("- "):
            resources.append(line[2:].strip())
            continue
        if line == "Resources:":
            line = "Resources: "
        fm = _FIELD_RE.match(line)
        key = fm.group("key").strip().lower() if fm else None
        if key not in _FIELD_ORDER:
            raise ValueError(f"the sign-in message has a line EIP-4361 does not allow: {line[:60]!r}")
        pos = _FIELD_ORDER.index(key)
        if pos <= last:
            raise ValueError(f"the sign-in field {fm.group('key')!r} is repeated or out of order")
        last = pos
        fields[key] = fm.group("value").strip()
    out["fields"] = fields
    out["resources"] = resources
    return out


def _classify_sign_in(parsed: Dict[str, Any], req: SignRequest, ctx: SignContext,
                      bound: str, now: float) -> SignVerdict:
    scheme = req.scheme
    want_family = "solana" if scheme == "solana_message" else "ethereum"
    domain = parsed["domain"]
    fields = parsed["fields"]
    host = origin_host(domain)
    decoded = {"kind": "siws" if want_family == "solana" else "siwe", "domain": host}
    if parsed["family"] != want_family:
        return _d(f"a Sign-In With {parsed['family'].title()} message cannot be signed "
                  f"with the {want_family.title()} key", scheme, **decoded)
    if not bound:
        return _d("a sign-in message needs the site it is for (the bound origin)", scheme,
                  **decoded)
    if not same_site(host, bound):
        return _d(f"the message signs in to {host!r}, but this request is bound to "
                  f"{bound!r} — a relayed sign-in message logs someone else in as this "
                  f"wallet", scheme, **decoded)
    address = parsed["address"]
    if want_family == "ethereum":
        if not _EVM_ADDR_RE.match(address) or address.lower() != str(ctx.holder).lower():
            return _d(f"the message names the address {address!r}, not this wallet "
                      f"({ctx.holder})", scheme, **decoded)
    elif not _SOL_ADDR_RE.match(address) or address != str(ctx.holder):
        return _d(f"the message names the address {address!r}, not this wallet "
                  f"({ctx.holder})", scheme, **decoded)
    uri = fields.get("uri", "")
    if want_family == "ethereum" or uri:
        if not same_site(origin_host(uri), host):
            return _d(f"the URI {uri!r} is not on {host!r}", scheme, **decoded)
    if want_family == "ethereum" and fields.get("version") != "1":
        return _d("the sign-in message has no 'Version: 1'", scheme, **decoded)
    if any(r.lower().startswith("urn:recap:") for r in parsed["resources"]):
        return SignVerdict(C, ("the sign-in message carries a ReCap (EIP-5573) — it delegates "
                               "capabilities, which is authority, not a login"), scheme, decoded)
    if _grants(parsed["statement"]) or _derives_key(parsed["statement"]):
        return SignVerdict(C, (f"the sign-in statement grants something "
                               f"({parsed['statement'][:80]!r}) — that is authority, not a login"),
                           scheme, decoded)
    nonce = fields.get("nonce", "")
    if not _NONCE_RE.match(nonce):
        return _d("the sign-in nonce is missing or shorter than 8 letters/digits", scheme,
                  **decoded)
    if want_family == "ethereum":
        try:
            chain_id = int(fields.get("chain id", ""))
        except ValueError:
            return _d("the sign-in message has no valid Chain ID", scheme, **decoded)
        allowed = ctx.chain_ids if ctx.chain_ids is not None else _default_chain_ids()
        if chain_id not in allowed:
            return _d(f"Chain ID {chain_id} is not a chain this wallet is pinned to", scheme,
                      **decoded)
        decoded["chain_id"] = chain_id
    issued = _parse_time(fields.get("issued at", "")) if fields.get("issued at") else None
    if fields.get("issued at") and issued is None:
        return _d("the sign-in Issued At is not a timestamp", scheme, **decoded)
    if issued is not None and issued > now + CLOCK_SKEW_SEC:
        return _d("the sign-in message is issued in the future", scheme, **decoded)
    if fields.get("expiration time"):
        expires = _parse_time(fields["expiration time"])
        if expires is None:
            return _d("the sign-in Expiration Time is not a timestamp", scheme, **decoded)
        if expires <= now:
            return _d("the sign-in message has already expired", scheme, **decoded)
        if expires - now > MAX_SIGN_IN_TTL_SEC:
            return _d(f"the sign-in message stays valid for more than "
                      f"{MAX_SIGN_IN_TTL_SEC // 3600} h", scheme, **decoded)
    else:
        # No Expiration Time (the siwe library default): the site's own nonce bounds
        # it, and it signs only when the site issued it just now.
        if issued is None:
            return _d("the sign-in message has neither an Expiration Time nor an Issued "
                      "At — an open-ended login signature is never signed", scheme, **decoded)
        if now - issued > MAX_OPEN_SIGN_IN_AGE_SEC:
            return _d(f"the sign-in message has no Expiration Time and was issued more "
                      f"than {MAX_OPEN_SIGN_IN_AGE_SEC // 60} min ago", scheme, **decoded)
        expires = issued + MAX_OPEN_SIGN_IN_AGE_SEC
        decoded["open_ended"] = True
    if fields.get("not before") and _parse_time(fields["not before"]) is None:
        return _d("the sign-in Not Before is not a timestamp", scheme, **decoded)
    decoded["expires"] = int(expires)
    data = req.payload.encode("utf-8")
    return SignVerdict(A, "a sign-in message for the site this request is bound to", scheme,
                       decoded, _digest(scheme, data), (host, nonce))


def classify(req: SignRequest, ctx: SignContext) -> SignVerdict:
    """The class of *req* for *ctx*. Never raises; any doubt is ``D``."""
    try:
        return _classify(req, ctx)
    except Exception as exc:  # noqa: BLE001 — a classifier bug refuses, never signs
        return _d(f"the request could not be classified ({type(exc).__name__}); refused",
                  getattr(req, "scheme", ""))


def _classify(req: SignRequest, ctx: SignContext) -> SignVerdict:
    scheme = req.scheme
    if scheme not in SCHEMES:
        return _d(f"unknown signing scheme {scheme!r}", scheme)
    if scheme == "eip712":
        return _d("typed data (EIP-712) is not signable yet: permits and orders need the "
                  "owner card or the spend gate (proposal 090 P1/P2)", scheme)
    if not isinstance(req.payload, str):
        return _d("the message must be text", scheme)
    if not str(ctx.holder or "").strip():
        return _d("no signing address", scheme)
    text = req.payload
    data = text.encode("utf-8")
    blind = _blind_text_reason(text, data)
    if blind:
        return _d(blind, scheme)
    now = time.time() if ctx.now is None else float(ctx.now)
    bound = origin_host(req.bound_origin)
    try:
        parsed = _parse_sign_in(text)
    except ValueError as exc:
        return _d(str(exc), scheme)
    if parsed is not None:
        return _classify_sign_in(parsed, req, ctx, bound, now)
    if "wants you to sign in with your" in text.lower():
        return _d("the text looks like a sign-in message but its header does not parse",
                  scheme)
    if _derives_key(text):
        return _d("the text derives or exports a key (an L2/trading/messaging account key) — "
                  "whoever holds that signature holds the key", scheme)
    if not bound:
        return SignVerdict(C, "the text is not bound to a site: name the origin it is for",
                           scheme, {"kind": "challenge"})
    fetch_bound = bool(req.fetch_bound and ctx.trust_fetch_bound)
    decoded = {"kind": "challenge", "domain": bound, "fetch_bound": fetch_bound}
    if _grants(text):
        return SignVerdict(C, ("the text authorizes, approves or delegates something — that "
                               "is authority, not a login"), scheme, decoded)
    named = _hosts_named(text)
    foreign = sorted(h for h in named if not same_site(h, bound))
    if foreign:
        return SignVerdict(C, (f"the text names {', '.join(foreign[:3])}, not only {bound!r} — "
                               f"a challenge relayed from another site would log this wallet "
                               f"in there"), scheme, decoded)
    digest = _digest(scheme, data)
    if names_host(text, bound) or any(same_site(h, bound) for h in named):
        return SignVerdict(A, f"a challenge for {bound}, and the text names the site", scheme,
                           decoded, digest)
    if fetch_bound:
        return SignVerdict(A, (f"a challenge {bound} returned to a request this tool made; the "
                               f"text does not name the site, so only a genuine owner turn "
                               f"signs it"), scheme, decoded, digest, owner_turn_only=True)
    return SignVerdict(C, (f"the text does not name {bound!r} and did not come from a "
                           f"request this tool made to it — signing it could prove this "
                           f"wallet to a different site. Use the fetch-bound form "
                           f"(wallet_sign_challenge), or the owner must approve it"),
                       scheme, decoded)


def _hosts_named(text: str) -> set:
    found = {h.lower().rstrip(".") for h in _URL_HOST_RE.findall(text)}
    found |= {h.lower().rstrip(".") for h in _BARE_HOST_RE.findall(text)}
    return {h for h in found if "." in h}


# -- signing -------------------------------------------------------------------

class SignRefused(ValueError):
    """The request is not class A, or the signer answered for another address."""

    def __init__(self, verdict: SignVerdict):
        self.verdict = verdict
        super().__init__(f"class {verdict.cls}: {verdict.reason}")


def sign_classified(signer, req: SignRequest, *, now: Optional[float] = None,
                    chain_ids: Optional[FrozenSet[int]] = None) -> Tuple[str, SignVerdict]:
    """Classify *req* for *signer*'s address and sign it when it is class A.

    ``(signature, verdict)``. A signer that carries the key in another process
    (``sign_classified`` on the signer object) is handed the wire request and
    re-classifies it there. EVM: EIP-191 ``personal_sign`` over the UTF-8 text,
    a 0x hex signature. Solana: ed25519 over the bytes, base58.
    """
    verdict = classify(req, SignContext(holder=signer.address, now=now, chain_ids=chain_ids))
    if not verdict.signable:
        raise SignRefused(verdict)
    remote = getattr(signer, "sign_classified", None)
    if callable(remote):
        signature = remote(req, verdict)
    else:
        raw = signer.sign_message(req.payload.encode("utf-8"))
        signature = _render_signature(req.scheme, raw)
    return signature, verdict


def _render_signature(scheme: str, raw) -> str:
    if scheme == "solana_message":
        return str(raw)  # solders.Signature renders base58
    text = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
    return text if text.startswith("0x") else "0x" + text
