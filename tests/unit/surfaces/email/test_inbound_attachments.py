"""Email attachments reach the agent (2026-09-13 chat-media rail).

``_plain_body`` walked the multipart tree for ``text/plain`` and returned; every
attachment was discarded with nothing said about it anywhere. An owner emailing a
PDF got an answer written as if the mail were empty.

Security boundary: email senders are correspondent-or-denied in v1 (a From: header
is forgeable). A CORRESPONDENT's attachment is therefore NAMED on the turn but its
bytes are never written into the workspace — only an owner-tier turn absorbs.
"""
from email.message import EmailMessage

import pytest

from surfaces.email.harness import normalize_email_message
from surfaces.email.inbound import build_inbound_message


class _Dir:
    def resolve_internal(self, addr, surface):
        return f"u_{addr}"


def _mail(*, body="hello", attachments=()):
    em = EmailMessage()
    em["From"] = "Someone <a@b.com>"
    em["Subject"] = "hi"
    em["Message-ID"] = "<m1@host>"
    em.set_content(body)
    for filename, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        em.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return em


def test_a_plain_mail_has_no_attachments():
    norm = normalize_email_message(_mail())
    assert norm["body"].strip() == "hello"
    assert norm["attachments"] == []


def test_an_attachment_is_extracted_with_its_name_mime_and_bytes():
    norm = normalize_email_message(_mail(
        attachments=[("report.pdf", "application/pdf", b"%PDF-1.4 body")]))
    assert norm["body"].strip() == "hello"
    assert len(norm["attachments"]) == 1
    att = norm["attachments"][0]
    assert att["filename"] == "report.pdf"
    assert att["mime"] == "application/pdf"
    assert att["data"] == b"%PDF-1.4 body"


def test_the_body_is_still_read_when_an_attachment_is_present():
    norm = normalize_email_message(_mail(
        body="see attached", attachments=[("a.png", "image/png", b"\x89PNG")]))
    assert "see attached" in norm["body"]


def test_several_attachments_all_survive():
    norm = normalize_email_message(_mail(attachments=[
        ("a.png", "image/png", b"one"),
        ("b.csv", "text/csv", b"two"),
    ]))
    assert [a["filename"] for a in norm["attachments"]] == ["a.png", "b.csv"]


def test_attachments_become_media_on_the_inbound_message():
    norm = normalize_email_message(_mail(
        attachments=[("a.png", "image/png", b"\x89PNGdata")]))
    inbound = build_inbound_message(norm, _Dir())
    assert [m.kind for m in inbound.media] == ["image"]
    assert inbound.media[0].filename == "a.png"
    assert inbound.media[0].data == b"\x89PNGdata"


def test_a_non_image_attachment_is_typed_as_a_document():
    norm = normalize_email_message(_mail(
        attachments=[("report.pdf", "application/pdf", b"%PDF")]))
    inbound = build_inbound_message(norm, _Dir())
    assert inbound.media[0].kind == "document"


def test_an_attachment_only_mail_still_names_the_file_in_the_text():
    """A body-less mail must not route as an empty turn — the same failure mode
    the Telegram photo hit."""
    norm = normalize_email_message(_mail(body="", attachments=[
        ("invoice.pdf", "application/pdf", b"%PDF")]))
    inbound = build_inbound_message(norm, _Dir())
    assert "invoice.pdf" in inbound.text
