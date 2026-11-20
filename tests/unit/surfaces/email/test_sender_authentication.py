"""CHAT-6: an email sender counts only when the receiving MX authenticated it.

The From header is parsed RAW (before RFC 2047 decoding), a header naming more
than one mailbox is refused, and the identity carries ``sender_authenticated``
only when the TOPMOST Authentication-Results shows dmarc=pass or an aligned
dkim/spf pass. resolve_access_tier keeps any other email sender at DENIED.
"""
import email

import pytest

from surfaces.email.inbound import build_inbound_message, parse_from_address
from tools.email_providers.mime import (
    KNOWN_AUTHSERV, authserv_ids_for_host, normalize_email_message, sender_address,
    sender_authenticated,
)

#: The test MX id (proposal 125 S35: the authserv-id is pinned per transport).
_TEST_MX = frozenset({"mx.example.org"})
_GOOGLE = frozenset({"mx.google.com"})


class _UD:
    def resolve_internal(self, raw, surface):
        return "u_" + raw


def _mail(headers: str) -> email.message.Message:
    return email.message_from_string(headers + "\nbody\n")


@pytest.mark.parametrize("ar,from_addr,ok", [
    ("mx.example.org; dmarc=pass (p=REJECT) header.from=acme.com", "j@acme.com", True),
    ("mx.example.org; dmarc=pass header.from=evil.com", "j@acme.com", False),
    ("mx.example.org; dkim=pass header.d=mail.acme.com; spf=fail", "j@acme.com", True),
    ("mx.example.org; dkim=pass header.d=evil.com", "j@acme.com", False),
    ("mx.example.org; spf=pass smtp.mailfrom=bounce@acme.com", "j@acme.com", True),
    ("mx.example.org; spf=pass smtp.mailfrom=bounce@evil.com", "j@acme.com", False),
    ("mx.example.org; dkim=pass header.d=com", "j@acme.com", False),
    ("mx.example.org; dmarc=fail header.from=acme.com", "j@acme.com", False),
    ("mx.example.org; none", "j@acme.com", False),
    ("", "j@acme.com", False),
    (None, "j@acme.com", False),
])
def test_sender_authenticated(ar, from_addr, ok):
    assert sender_authenticated(ar, from_addr, authserv_ids=_TEST_MX) is ok


def test_encoded_word_cannot_smuggle_a_second_address():
    raw = "=?utf-8?q?boss=40acme=2Ecom?= <attacker@evil.com>"
    assert sender_address(raw) == "attacker@evil.com"
    assert parse_from_address(raw) == "attacker@evil.com"


def test_header_with_two_mailboxes_is_refused():
    assert sender_address("a@acme.com, b@evil.com") == ""


def test_two_from_headers_name_no_sender():
    em = _mail("From: a@acme.com\nFrom: b@evil.com\nMessage-ID: <1@x>\n")
    assert normalize_email_message(em)["from"] == ""


def test_only_the_topmost_authentication_results_counts():
    # The MX prepends its own header ABOVE any the sender wrote.
    em = _mail("Authentication-Results: mx.example.org; dmarc=fail header.from=acme.com\n"
               "Authentication-Results: forged; dmarc=pass header.from=acme.com\n"
               "From: John <j@acme.com>\nMessage-ID: <2@x>\n")
    assert normalize_email_message(em, authserv_ids=_TEST_MX)["sender_authenticated"] is False


def test_authenticated_mail_reaches_the_identity():
    em = _mail("Authentication-Results: mx.example.org; dmarc=pass header.from=acme.com\n"
               "From: John <j@acme.com>\nMessage-ID: <3@x>\n")
    msg = normalize_email_message(em, authserv_ids=_TEST_MX)
    assert msg["sender_authenticated"] is True
    inbound = build_inbound_message(msg, _UD())
    assert inbound.identity.sender_authenticated is True


def test_unauthenticated_mail_identity_is_not_authenticated():
    em = _mail("From: John <j@acme.com>\nMessage-ID: <4@x>\n")
    inbound = build_inbound_message(normalize_email_message(em), _UD())
    assert inbound.identity.sender_authenticated is False


# The real Gmail IMAP shape (prod 2026-10-08: 165/165 external INBOX mails
# carry it): mx.google.com on top, folded, with comments and header.i=@domain.
_GMAIL_OWNER = (
    "Delivered-To: agent@example.org\n"
    "ARC-Authentication-Results: i=1; mx.google.com;\n"
    "       dkim=pass header.i=@gmail.com header.s=20230601 header.b=Ab+c/1;\n"
    "       dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com\n"
    "Return-Path: <owner@gmail.com>\n"
    "Received-SPF: pass (google.com: domain of owner@gmail.com designates "
    "209.85.220.41 as permitted sender) client-ip=209.85.220.41;\n"
    "Authentication-Results: mx.google.com;\n"
    "       dkim=pass header.i=@gmail.com header.s=20230601 header.b=Ab+c/1;\n"
    "       spf=pass (google.com: domain of owner@gmail.com designates "
    "209.85.220.41 as permitted sender) smtp.mailfrom=owner@gmail.com;\n"
    "       dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com;\n"
    "       dara=pass header.i=@gmail.com\n"
    "From: The Owner <owner@gmail.com>\nMessage-ID: <g1@mail.gmail.com>\n")


def test_real_gmail_owner_mail_is_authenticated():
    assert normalize_email_message(_mail(_GMAIL_OWNER),
                                   authserv_ids=_GOOGLE)["sender_authenticated"] is True


def test_gmail_shape_with_failed_dmarc_stays_unauthenticated():
    # prod: a domain with a broken SPF record and an unaligned DKIM signature
    hdr = ("Authentication-Results: mx.google.com;\n"
           "       dkim=pass header.i=@relay.example header.s=s1;\n"
           "       spf=permerror (google.com: permanent error) smtp.mailfrom=a@acme.com;\n"
           "       dmarc=fail (p=NONE) header.from=acme.com\n"
           "From: a@acme.com\nMessage-ID: <g2@x>\n")
    assert normalize_email_message(_mail(hdr),
                                   authserv_ids=_GOOGLE)["sender_authenticated"] is False


@pytest.mark.parametrize("ar,ok", [
    # Microsoft 365 writes no authserv-id: the first clause is already a result.
    ("spf=pass (sender IP is 209.85.1.1) smtp.mailfrom=acme.com; dkim=none "
     "(message not signed) header.d=none;dmarc=none action=none header.from=acme.com",
     True),
    ("spf=fail (sender IP is 6.6.6.6) smtp.mailfrom=acme.com; dkim=none; "
     "dmarc=fail action=none header.from=acme.com", False),
    ("spf=pass smtp.mailfrom=evil.com; dmarc=none header.from=acme.com", False),
])
def test_microsoft_shape_without_authserv_id(ar, ok):
    assert sender_authenticated(ar, "j@acme.com", authserv_ids=frozenset({""})) is ok


# --- proposal 125 S35 (D10): the authserv-id is pinned per transport -----------


def test_attacker_authserv_id_with_dmarc_pass_is_refused():
    # A self-hosted or relaying MX adds no header: the topmost one is the attacker's.
    ar = "attacker.example; dmarc=pass header.from=acme.com"
    assert sender_authenticated(ar, "j@acme.com", authserv_ids=_GOOGLE) is False


def test_attacker_header_above_gmail_shape_is_refused():
    em = _mail("Authentication-Results: attacker.example; dmarc=pass header.from=gmail.com\n"
               "From: The Owner <owner@gmail.com>\nMessage-ID: <g3@x>\n")
    assert normalize_email_message(em, authserv_ids=_GOOGLE)["sender_authenticated"] is False


def test_versioned_authserv_id_matches():
    ar = "mx.google.com 1; dmarc=pass header.from=acme.com"
    assert sender_authenticated(ar, "j@acme.com", authserv_ids=_GOOGLE) is True


def test_no_ids_is_refused():
    ar = "mx.google.com; dmarc=pass header.from=acme.com"
    assert sender_authenticated(ar, "j@acme.com", authserv_ids=frozenset()) is False
    em = _mail("Authentication-Results: " + ar + "\nFrom: j@acme.com\nMessage-ID: <5@x>\n")
    assert normalize_email_message(em)["sender_authenticated"] is False  # default: no pin


def test_empty_id_pin_refuses_a_named_id():
    ar = "mx.google.com; dmarc=pass header.from=acme.com"
    assert sender_authenticated(ar, "j@acme.com", authserv_ids=frozenset({""})) is False


@pytest.mark.parametrize("host,ids", [
    ("imap.gmail.com", {"mx.google.com"}),
    ("imap.googlemail.com", {"mx.google.com"}),
    ("IMAP.Gmail.com.", {"mx.google.com"}),
    ("outlook.office365.com", {""}),
    ("imap-mail.outlook.com", {""}),
    ("imap.hotmail.com", {""}),
    ("mail.self-hosted.example", set()),
    ("agentmail", set()),
    ("", set()),
    (None, set()),
])
def test_authserv_ids_for_host(host, ids):
    assert authserv_ids_for_host(host) == frozenset(ids)


def test_agentmail_fails_closed_until_pinned():
    """Audit 2026-11-21 M-2: AgentMail's authserv-id waits for a real message
    sample from a box. Until then the row is the EMPTY set: no AgentMail sender
    is authenticated (it was ``None`` = any id, which let a forged header in).
    Pin it when a sample exists, and this test changes with it."""
    assert KNOWN_AUTHSERV["agentmail"] == frozenset()
    ar = "any-id.example; dmarc=pass header.from=acme.com"
    assert sender_authenticated(ar, "j@acme.com",
                                authserv_ids=KNOWN_AUTHSERV["agentmail"]) is False
