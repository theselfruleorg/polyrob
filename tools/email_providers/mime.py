"""Parse one RFC 5322 message into the plain dicts the email rail uses.

ONE parser for both halves of the rail: the inbound surface
(``surfaces/email/harness.py`` re-exports these names) and the agent's own
mailbox verbs (``tools/email_mailbox.py``). Pure: no network, no state.
"""
import logging
import re
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parseaddr
from typing import Optional

logger = logging.getLogger(__name__)


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = []
        for chunk, enc in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts)
    except Exception:
        return value


#: What the turn text says when an email carried no body we could read. Named,
#: never blank: an empty turn is indistinguishable from a message that said
#: nothing, and the agent answers it as though the sender wrote nothing.
NO_BODY_NOTE = "[this email had no readable text body]"

#: The most HTML one mail body may hand the stripper (CHAT-7). The stripper is
#: linear, so this bounds memory and time, not a quadratic blow-up.
HTML_MAX_CHARS = 512 * 1024

_HIDDEN_TAGS = ("script", "style")


def strip_html(html: str) -> str:
    """Render mail as text in ONE linear pass; ignore script and style bodies.

    ⚠️ CHAT-7: the stdlib ``HTMLParser`` is quadratic on unterminated markup
    (``"<a" * N`` took minutes at 200 KB) and this runs on mail anyone can
    send. Every scan here moves forward with ``str.find``; an unterminated tag
    ends the markup. The input is capped at :data:`HTML_MAX_CHARS`.
    """
    import html as _html

    src = (html or "")[:HTML_MAX_CHARS]
    n = len(src)
    out = []
    hidden = None
    i = 0
    while i < n:
        lt = src.find("<", i)
        if lt < 0:
            if not hidden:
                out.append(src[i:])
            break
        if not hidden:
            out.append(src[i:lt])
        if src.startswith("<!--", lt):
            end = src.find("-->", lt + 4)
            if end < 0:
                break
            i = end + 3
            continue
        nxt = src[lt + 1:lt + 2]
        if not (nxt.isalpha() or nxt in ("/", "!", "?")):
            if not hidden:
                out.append("<")          # a bare "<" in text ("a < b")
            i = lt + 1
            continue
        gt = src.find(">", lt + 1)
        if gt < 0:
            break                         # unterminated markup: nothing more to read
        body = src[lt + 1:gt]
        closing = body.startswith("/")
        name = body.lstrip("/!?").split(None, 1)[0].rstrip("/").lower() if body.strip("/!? ") else ""
        if hidden:
            if closing and name == hidden:
                hidden = None
                out.append(" ")
        elif not closing and name in _HIDDEN_TAGS:
            hidden = name
        elif closing:
            out.append("\n\n" if name == "p" else " ")
        else:
            out.append("\n" if name == "br" else " ")
        i = gt + 1
    text = re.sub(r"[ \t\r\f\v]+", " ", _html.unescape("".join(out)))
    return re.sub(r"\n(?: *\n)+", "\n\n", text).strip()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _plain_body(em: Message) -> str:
    """The message's readable text: ``text/plain``, else ``text/html`` stripped,
    else :data:`NO_BODY_NOTE` — never a silent empty string (D27)."""
    try:
        html = ""
        if em.is_multipart():
            for part in em.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    text = _decode_part(part)
                    if text.strip():
                        return text
                elif ctype == "text/html" and not html:
                    html = _decode_part(part)
        else:
            text = _decode_part(em) or (em.get_payload() or "")
            if em.get_content_type() == "text/html":
                html, text = text, ""
            if text.strip():
                return text
        if html.strip():
            stripped = strip_html(html)
            if stripped:
                return stripped
        return NO_BODY_NOTE
    except Exception as e:
        logger.warning("email body extraction failed: %s", e, exc_info=True)
        return NO_BODY_NOTE


def _attachments(em: Message) -> list:
    """Every attached part as ``{filename, mime, data}`` (2026-09-13 media rail).

    ``_plain_body`` above returns at the FIRST ``text/plain`` part and everything
    else was discarded — an owner emailing a PDF got an answer written as if the
    mail were empty. Only parts with an explicit attachment disposition or a
    filename are taken, so the body and its ``text/html`` twin never show up here.
    """
    out: list = []
    try:
        if not em.is_multipart():
            return out
        for part in em.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            if disposition != "attachment" and not filename:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception as e:
                # D64: a part whose transfer-encoding we cannot decode is still
                # an attachment the sender sent. Keeping it with ``data=None``
                # makes the rail NAME it and say it could not be read; dropping
                # it made the agent answer as if it were never attached.
                logger.warning("email attachment %r could not be decoded: %s",
                               filename, e)
                payload = None
            out.append({
                "filename": _decode(filename) if filename else None,
                "mime": part.get_content_type(),
                "data": payload or None,
            })
    except Exception as e:  # a malformed MIME tree must never lose the whole mail
        logger.debug("email attachment extraction failed: %s", e)
    return out


#: Headers that mark a mail as machine-sent (OS5).
_AUTO_HEADERS = ("Auto-Submitted", "Precedence", "X-Autoreply", "X-Autorespond")
_AUTO_PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})
_AUTO_SENDERS = frozenset({"mailer-daemon", "postmaster"})


def auto_headers(em: Message) -> dict:
    """The OS5 markers present on ``em`` — the ``headers`` key of a normalized mail."""
    return {k: str(em.get(k) or "") for k in _AUTO_HEADERS if em.get(k)}


def is_auto_generated(msg: dict) -> bool:
    """True for a machine-sent mail the agent must never answer (OS5).

    RFC 3834: an ``Auto-Submitted`` value other than ``no``; the de-facto
    ``Precedence: bulk|junk|list|auto_reply``, ``X-Autoreply`` and
    ``X-Autorespond``; and a bounce from ``MAILER-DAEMON``/``postmaster``.
    Answering one starts an agent <-> auto-responder loop. The ONE classifier:
    the email surface refuses to answer these, and ``email_read_machine_mail``
    returns them to the agent (work order 0009).
    """
    headers = {str(k).lower(): str(v or "").strip().lower()
               for k, v in (msg.get("headers") or {}).items()}
    auto = headers.get("auto-submitted", "")
    if auto and auto != "no":
        return True
    if headers.get("precedence", "") in _AUTO_PRECEDENCE:
        return True
    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return True
    _, addr = parseaddr(msg.get("from", "") or "")
    return (addr or "").strip().lower().split("@", 1)[0] in _AUTO_SENDERS


def _raw_headers(em: Message, name: str) -> list:
    """Every RAW (undecoded) value of header ``name``, top to bottom."""
    want = name.lower()
    return [str(v) for k, v in em.raw_items() if str(k).lower() == want]


def sender_address(raw_from: str) -> str:
    """The ONE bare sender address in a RAW ``From`` value, lower-cased; ``''``
    when it names none or more than one.

    ⚠️ Parse the raw header, never a decoded one: an RFC 2047 encoded-word in
    the display name decodes into text that can itself look like a second
    address, and ``parseaddr`` then picks the attacker's (CHAT-6). A header
    that names several mailboxes is refused rather than guessed at.
    """
    unfolded = re.sub(r"\r?\n[ \t]", " ", str(raw_from or ""))
    try:
        found = [a for _, a in getaddresses([unfolded]) if a]
    except Exception:
        return ""
    if len(found) != 1 or "@" not in found[0]:
        return ""
    return found[0].strip().lower()


def _aligned(domain: str, from_domain: str) -> bool:
    """Relaxed DMARC-style alignment: equal, or one is a subdomain of the other
    (the shorter side must still be a registrable-looking name, not a bare TLD)."""
    a = (domain or "").strip().strip(".").lower()
    b = (from_domain or "").strip().strip(".").lower()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return "." in short and long_.endswith("." + short)


_AR_RESULT_RE = re.compile(r"^\s*([a-z0-9_-]+)\s*=\s*([a-z]+)\b(.*)$", re.I | re.S)
_AR_PROP_RE = re.compile(
    r"\b(header\.from|header\.d|header\.i|smtp\.mailfrom)\s*=\s*\"?([^\s;\"]+)", re.I)


def sender_authenticated(auth_results: Optional[str], from_addr: str) -> bool:
    """True only when ``auth_results`` (the TOPMOST ``Authentication-Results``
    header — the one the receiving MX added) proves ``from_addr``'s domain:
    ``dmarc=pass`` for that From domain, or an ALIGNED ``dkim=pass`` /
    ``spf=pass``. Anything else — absent, unparseable, ``fail``, ``none``,
    an unaligned pass — is False. Fail-closed by construction (CHAT-6).
    """
    domain = (from_addr or "").rpartition("@")[2].strip().lower()
    if not auth_results or not domain:
        return False
    try:
        text = re.sub(r"\([^()]*\)", " ", str(auth_results))
        parts = text.split(";")
        # [0] is the authserv-id (+ version) — except on Microsoft 365, which
        # writes no authserv-id and opens with a result ("spf=pass ...").
        for part in (parts if _AR_RESULT_RE.match(parts[0]) else parts[1:]):
            m = _AR_RESULT_RE.match(part)
            if not m or m.group(2).lower() != "pass":
                continue
            method = m.group(1).lower()
            props = {k.lower(): v.strip().lower() for k, v in _AR_PROP_RE.findall(m.group(3))}
            if method == "dmarc":
                hf = props.get("header.from")
                if hf is None or _aligned(hf.rpartition("@")[2], domain):
                    return True
            elif method == "dkim":
                d = props.get("header.d") or props.get("header.i", "").rpartition("@")[2]
                if _aligned(d, domain):
                    return True
            elif method == "spf":
                if _aligned(props.get("smtp.mailfrom", "").rpartition("@")[2], domain):
                    return True
    except Exception as e:  # a malformed header proves nothing
        logger.debug("Authentication-Results parse failed: %s", e)
    return False


def normalize_email_message(em: Message) -> dict:
    """Map a parsed email to the normalized dict ``process_email`` consumes. Pure."""
    froms = _raw_headers(em, "From")
    # Parse sender identity before any encoded display-name decoding. Two From
    # headers name no single sender: the mail carries no usable identity.
    raw_from = froms[0] if len(froms) == 1 else ""
    auth = _raw_headers(em, "Authentication-Results")
    return {
        "message_id": (em.get("Message-ID") or "").strip(),
        "from": raw_from,
        # CHAT-6: the sender counts as authenticated only on the receiving MX's
        # (topmost) Authentication-Results; a forged From: is never a contact.
        "sender_authenticated": sender_authenticated(
            auth[0] if auth else None, sender_address(raw_from)),
        "subject": _decode(em.get("Subject")),
        "body": _plain_body(em),
        "in_reply_to": (em.get("In-Reply-To") or "").strip(),
        "references": (em.get("References") or "").strip(),
        "attachments": _attachments(em),
        # OS5: the RFC 3834 / de-facto auto-reply markers, read by
        # ``is_auto_generated`` so an auto-responder is never answered.
        "headers": auto_headers(em),
    }
