"""Read the receiving MX's ``Authentication-Results`` header (RFC 8601). Pure.

The ONE reader of sender proof for the email rail (CHAT-6, proposal 125 S35,
audit 2026-11-21 M-2/M-7/L-18). ``tools/email_providers/mime.py`` re-exports
these names; callers import them from there.

⚠️ Fail closed everywhere: an unknown transport, a header whose order is not
known, an unterminated quote or comment, a clause that is not a result, a
domain that is not a bare domain — each proves nothing.
"""
import logging
import re
from typing import Any, Dict, FrozenSet, List, Optional

logger = logging.getLogger(__name__)


#: Proposal 125 S35 (D10): the authserv-id the receiving MX of each transport
#: writes, keyed by the registrable domain of the mailbox host. Only the TOPMOST
#: ``Authentication-Results`` whose authserv-id is in the row counts: a host that
#: adds no header of its own (a self-hosted server) would otherwise let the
#: sender's forged header be the topmost one. ``""`` is the Microsoft shape
#: (no authserv-id; the header opens with a result). A host not in the table
#: fails closed (no sender is authenticated; email senders stay at DENIED).
#: ⚠️ ``"agentmail"`` is the EMPTY set until a real message sample from a box
#: pins AgentMail's id (audit 2026-11-21 M-2): no AgentMail sender is
#: authenticated. Put the captured id in the set; never ``None`` ("any id").
#: No env setting on purpose (D10); add ``EMAIL_AUTHSERV_ID`` only on an owner ask.
KNOWN_AUTHSERV: Dict[str, FrozenSet[str]] = {
    "gmail.com": frozenset({"mx.google.com"}),
    "googlemail.com": frozenset({"mx.google.com"}),
    "outlook.com": frozenset({""}),
    "office365.com": frozenset({""}),
    "hotmail.com": frozenset({""}),
    "agentmail": frozenset(),
}


def authserv_ids_for_host(host: Optional[str]) -> FrozenSet[str]:
    """The accepted authserv-ids for an IMAP host (``imap.gmail.com`` ->
    ``{"mx.google.com"}``); an empty set (fail closed) for any other host."""
    labels = [x for x in str(host or "").strip().strip(".").lower().split(".") if x]
    if len(labels) < 2:
        return frozenset()
    return KNOWN_AUTHSERV.get(".".join(labels[-2:])) or frozenset()


def topmost_header(headers: Any, name: str) -> Optional[str]:
    """The FIRST (topmost) value of header ``name`` in an ORDERED header shape:
    a list of ``(name, value)`` pairs or ``{"name", "value"}`` dicts, or a
    mapping whose value is a list. ``None`` for anything else.

    ⚠️ A plain ``{name: value}`` mapping returns ``None`` (audit M-2): it cannot
    show which header was on top, and a JSON object with a repeated key keeps
    only one of them — the sender's forged header can be the one kept.
    """
    want = name.lower()
    if isinstance(headers, dict):
        found = [v for k, v in headers.items() if str(k).lower() == want]
        if len(found) == 1 and isinstance(found[0], (list, tuple)) and found[0]:
            return str(found[0][0])
        return None
    if not isinstance(headers, (list, tuple)):
        return None
    for item in headers:
        if isinstance(item, dict):
            key, value = item.get("name"), item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            key, value = item
        else:
            return None  # an unknown shape: its order means nothing
        if str(key).lower() == want:
            return None if value is None else str(value)
    return None


# --- the RFC 8601 tokenizer (M-7) ---------------------------------------------

def _clauses(text: str) -> Optional[List[List[str]]]:
    """Split a header into ``;`` clauses of words. A ``;`` or a space inside a
    quoted string never splits (the quotes stay in the word); a comment, nested
    to any depth, is dropped. ``None`` on an unterminated quote or comment."""
    clauses: List[List[str]] = [[]]
    word: List[str] = []
    depth, quoted, i, n = 0, False, 0, len(text)

    def flush() -> None:
        if word:
            clauses[-1].append("".join(word))
            word.clear()

    while i < n:
        c = text[i]
        if quoted:
            word.append(c)
            if c == "\\" and i + 1 < n:
                word.append(text[i + 1])
                i += 1
            elif c == '"':
                quoted = False
        elif depth:
            if c == "\\":
                i += 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
        elif c == '"':
            quoted = True
            word.append(c)
        elif c == "(":
            flush()
            depth = 1
        elif c == ";":
            flush()
            clauses.append([])
        elif c.isspace():
            flush()
        else:
            word.append(c)
        i += 1
    if quoted or depth:
        return None
    flush()
    return [_join_equals(c) for c in clauses]


def _join_equals(words: List[str]) -> List[str]:
    """``dkim = pass`` -> ``dkim=pass`` (RFC 8601 allows CFWS around ``=``)."""
    out: List[str] = []
    for w in words:
        if out and (w.startswith("=") or out[-1].endswith("=")):
            out[-1] += w
        else:
            out.append(w)
    return out


_RESULT_RE = re.compile(r"^([a-z0-9_-]+)(?:/[0-9]+)?=([a-z]+)$", re.I)
_PROP_RE = re.compile(r"^([a-z0-9_-]+(?:\.[a-z0-9_-]+)*)=(.*)$", re.I | re.S)
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _result(words: List[str]):
    """``(method, result, props)`` of one result clause; ``None`` when the clause
    is not one (a stray word is never skipped: it may be an injection's tail)."""
    m = _RESULT_RE.match(words[0])
    if not m:
        return None
    props: Dict[str, str] = {}
    for w in words[1:]:
        p = _PROP_RE.match(w)
        if not p:
            return None
        props.setdefault(p.group(1).lower(), p.group(2))
    return m.group(1).lower(), m.group(2).lower(), props


def _bare_domain(value: Optional[str], *, at_ok: bool = False,
                 local_ok: bool = False) -> str:
    """``value`` as a lower-cased bare domain, or ``""``. ``at_ok``: ``@domain``
    too; ``local_ok``: ``local@domain`` too (the local part is never read)."""
    v = (value or "").strip().lower()
    if "@" in v:
        local, _, v = v.rpartition("@")
        if not (local_ok or (at_ok and not local)):
            return ""
    return v if _DOMAIN_RE.match(v) else ""


# --- alignment (L-18) ---------------------------------------------------------

#: Multi-label public suffixes (no PSL library in the lock, on purpose): an
#: alignment never stops at one of these, nor at a single label. Not the whole
#: list — a name missing here aligns only by exact match or a deeper subdomain.
_MULTI_PART_SUFFIXES = frozenset({
    "ac.uk", "co.uk", "gov.uk", "ltd.uk", "me.uk", "net.uk", "org.uk", "plc.uk",
    "com.au", "edu.au", "gov.au", "net.au", "org.au", "co.nz", "net.nz", "org.nz",
    "ac.jp", "co.jp", "go.jp", "ne.jp", "or.jp", "co.kr", "or.kr", "com.br",
    "net.br", "org.br", "com.cn", "gov.cn", "net.cn", "org.cn", "com.hk",
    "com.tw", "com.sg", "com.my", "co.in", "net.in", "org.in", "co.za",
    "org.za", "com.mx", "com.ar", "com.tr", "co.il", "org.il", "com.ua",
    "co.id", "com.ph", "com.vn", "com.pk", "com.ng", "com.eg", "com.sa",
    "co.th", "ac.th", "com.co", "com.pe", "com.pl", "co.at", "or.at",
    "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "blogspot.com",
    "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "cloudfront.net", "azurewebsites.net", "amazonaws.com",
    "onrender.com", "fly.dev", "glitch.me",
})


def _is_suffix(name: str) -> bool:
    return name.count(".") < 1 or name in _MULTI_PART_SUFFIXES


def _aligned(domain: str, from_domain: str) -> bool:
    """Relaxed DMARC-style alignment: equal, or one is a subdomain of the other,
    and the shared name is never a public suffix (``co.uk``, ``github.io``)."""
    a = (domain or "").strip().strip(".").lower()
    b = (from_domain or "").strip().strip(".").lower()
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if _is_suffix(short):
        return False
    return short == long_ or long_.endswith("." + short)


# --- the verdict --------------------------------------------------------------

def sender_authenticated(auth_results: Optional[str], from_addr: str, *,
                         authserv_ids: FrozenSet[str]) -> bool:
    """True only when ``auth_results`` (the TOPMOST ``Authentication-Results``
    header — the one the receiving MX added) proves ``from_addr``'s domain:
    ``dmarc=pass`` with an aligned ``header.from``, or an ALIGNED ``dkim=pass`` /
    ``spf=pass``. Anything else — absent, unparseable, ``fail``, ``none``, an
    unaligned pass, a malformed clause anywhere — is False (CHAT-6).

    ``authserv_ids`` (125 S35): the header's authserv-id must be in this set; an
    empty set is always False.
    """
    domain = (from_addr or "").rpartition("@")[2].strip().lower()
    if not auth_results or not domain or not authserv_ids:
        return False
    try:
        clauses = _clauses(str(auth_results))
        if clauses is None or not clauses[0]:
            return False
        head = clauses[0]
        if _RESULT_RE.match(head[0]):
            # Microsoft 365 writes no authserv-id: the header opens with a result.
            ident, results = "", clauses
        else:
            ident, results = head[0].lower(), clauses[1:]
        if ident not in authserv_ids:
            return False
        parsed = []
        for words in results:
            if not words or words == ["none"]:
                continue
            r = _result(words)
            if r is None:
                return False  # a clause that is no result: the header proves nothing
            parsed.append(r)
        for method, result, props in parsed:
            if result != "pass":
                continue
            if method == "dmarc":
                d = _bare_domain(props.get("header.from"), local_ok=True)
            elif method == "dkim":
                d = (_bare_domain(props.get("header.d"))
                     if "header.d" in props
                     else _bare_domain(props.get("header.i"), at_ok=True))
            elif method == "spf":
                d = _bare_domain(props.get("smtp.mailfrom"), local_ok=True)
            else:
                continue
            if d and _aligned(d, domain):
                return True
    except Exception as e:  # a malformed header proves nothing
        logger.debug("Authentication-Results parse failed: %s", e)
    return False
