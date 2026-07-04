"""WS-B email harness — the pure IMAP-message normalizer (network-free)."""
from email import message_from_string

from surfaces.email.harness import normalize_email_message


_RAW = (
    "From: John Doe <john@acme.com>\r\n"
    "To: rob@bot.com\r\n"
    "Subject: Re: invoice\r\n"
    "Message-ID: <reply1@acme.com>\r\n"
    "In-Reply-To: <out1@rob>\r\n"
    "References: <root@rob> <out1@rob>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "The invoice is paid.\r\n"
    "\r\n"
    "On Mon ROB wrote:\r\n"
    "> please confirm\r\n"
)


def test_normalize_captures_headers_and_plain_body():
    em = message_from_string(_RAW)
    msg = normalize_email_message(em)
    assert msg["message_id"] == "<reply1@acme.com>"
    assert msg["from"] == "John Doe <john@acme.com>"
    assert msg["in_reply_to"] == "<out1@rob>"
    assert msg["references"] == "<root@rob> <out1@rob>"
    assert "The invoice is paid." in msg["body"]


def test_normalize_multipart_prefers_plain_text():
    raw = (
        "From: a@b.com\r\n"
        "Subject: hi\r\n"
        "Message-ID: <m@x>\r\n"
        'Content-Type: multipart/alternative; boundary="B"\r\n'
        "\r\n"
        "--B\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "plain body here\r\n"
        "--B\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
        "<p>html body</p>\r\n"
        "--B--\r\n"
    )
    msg = normalize_email_message(message_from_string(raw))
    assert "plain body here" in msg["body"]
    assert "<p>" not in msg["body"]
