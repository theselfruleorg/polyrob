"""D4/D26-D30/D64-D65/D70 — the email rail never answers an outage with silence.

Every test here pins a failure the 2026-09-21 interface audit found: a message
that was marked handled before it was dispatched, an HTML-only mail that routed
as an empty turn, an attachment that vanished, and an inbound outage that read
exactly like "no new mail".
"""
import asyncio
import os
from email.message import EmailMessage

import pytest

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import MailFetchError, normalize_agentmail_attachments
from surfaces.email.harness import (
    NO_BODY_NOTE, EmailHarness, normalize_email_message, strip_html,
)
from surfaces.email.inbound import _attachment_media, _append_attachment_manifest


# --- D27: an HTML-only body is read, never dropped --------------------------

def _html_only() -> EmailMessage:
    em = EmailMessage()
    em["From"] = "her@example.com"
    em["Subject"] = "the quote"
    em["Message-ID"] = "<h1@x>"
    em.set_content("<html><body><p>Ship it</p><br>by friday</body></html>",
                   subtype="html")
    return em


def test_an_html_only_email_yields_readable_text():
    body = normalize_email_message(_html_only())["body"]
    assert "Ship it" in body
    assert "by friday" in body
    assert "<p>" not in body


def test_strip_html_drops_script_and_unescapes():
    out = strip_html("<script>evil()</script><p>a &amp; b</p>")
    assert "evil" not in out
    assert "a & b" in out


def test_a_body_that_cannot_be_read_is_NAMED_not_blank():
    """A multipart with no text part at all: the turn SAYS so.

    An empty string here is indistinguishable from a sender who wrote nothing,
    and the agent answers it as though they had.
    """
    em = EmailMessage()
    em["From"] = "x@y.z"
    em.set_content("placeholder")
    em.make_mixed()
    em.set_payload([p for p in em.get_payload()
                    if p.get_content_type() != "text/plain"])
    assert normalize_email_message(em)["body"] == NO_BODY_NOTE


# --- D64: an unreadable attachment is kept and named ------------------------

def test_an_attachment_without_bytes_is_kept_and_reported():
    media = _attachment_media([{"filename": "deck.pdf", "mime": "application/pdf",
                                "data": None}])
    assert len(media) == 1
    text = _append_attachment_manifest("see attached", media)
    assert "deck.pdf" in text
    assert "could not be read" in text


# --- D26: AgentMail attachments map onto the ONE shape ----------------------

def test_agentmail_attachments_normalize_to_the_shared_shape():
    import base64
    raw = [{"filename": "a.png", "content_type": "image/png",
            "content": base64.b64encode(b"PNGDATA").decode()},
           {"filename": "b.pdf", "content_type": "application/pdf"}]
    out = normalize_agentmail_attachments(raw)
    assert out[0] == {"filename": "a.png", "mime": "image/png", "data": b"PNGDATA"}
    # metadata-only: kept with data=None so the turn can NAME it (D64)
    assert out[1]["filename"] == "b.pdf" and out[1]["data"] is None


def test_agentmail_attachments_tolerate_nothing():
    assert normalize_agentmail_attachments(None) == []


# --- D4: handled only AFTER dispatch ----------------------------------------

class _Fetcher:
    def __init__(self, messages, *, boom=None):
        self._messages = messages
        self.marked = []
        self._boom = boom

    async def fetch_unread(self):
        if self._boom is not None:
            raise self._boom
        return list(self._messages)

    def mark_handled(self, handle):
        self.marked.append(handle)


def _harness(tmp_path, fetcher, *, router):
    h = EmailHarness.__new__(EmailHarness)
    h.container = None
    h.task_agent = None
    h.email_tool = None
    h.poll_interval = 60.0
    h.dedup = MessageDedup(os.path.join(str(tmp_path), "dedup.db"))
    h.fetcher = fetcher
    h.user_directory = object()
    h.surface = None
    h._stop = False
    h._attempts = {}
    h._route = router
    return h


def _install_route(monkeypatch, fn):
    """`poll_once` imports `act_on_inbound` from the telegram harness."""
    import surfaces.telegram.harness as th
    monkeypatch.setattr(th, "act_on_inbound", fn)


@pytest.mark.asyncio
async def test_a_failed_route_leaves_the_message_unhandled(tmp_path, monkeypatch):
    """D4: nothing is marked until it has been dispatched.

    The old order recorded the dedup key and set \\Seen in a `finally`, so one
    raising route lost the mail permanently with a debug line as its only trace.
    """
    msg = {"message_id": "<m1@x>", "from": "a@b.c", "subject": "s", "body": "b"}
    fetcher = _Fetcher([("uid1", msg)])
    h = _harness(tmp_path, fetcher, router=None)

    async def _boom(*a, **kw):
        raise RuntimeError("router down")

    _install_route(monkeypatch, _boom)
    import surfaces.email.harness as eh
    monkeypatch.setattr(eh, "process_email", _boom)

    assert await h.poll_once() == 0
    assert fetcher.marked == []                     # not \Seen
    assert h.dedup.was_seen("<m1@x>") is False      # not deduped


@pytest.mark.asyncio
async def test_a_poison_message_is_given_up_on_after_a_bounded_retry(tmp_path, monkeypatch):
    msg = {"message_id": "<m2@x>", "from": "a@b.c", "subject": "s", "body": "b"}
    fetcher = _Fetcher([("uid2", msg)])
    h = _harness(tmp_path, fetcher, router=None)

    async def _boom(*a, **kw):
        raise RuntimeError("always")

    import surfaces.email.harness as eh
    monkeypatch.setattr(eh, "process_email", _boom)
    for _ in range(3):
        await h.poll_once()
    assert fetcher.marked == ["uid2"]
    assert h.dedup.was_seen("<m2@x>") is True


@pytest.mark.asyncio
async def test_a_dispatched_message_is_marked_once(tmp_path, monkeypatch):
    msg = {"message_id": "<m3@x>", "from": "a@b.c", "subject": "s", "body": "b"}
    fetcher = _Fetcher([("uid3", msg)])
    h = _harness(tmp_path, fetcher, router=None)

    async def _ok(*a, **kw):
        return object()

    async def _act(*a, **kw):
        return True

    import surfaces.email.harness as eh
    monkeypatch.setattr(eh, "process_email", _ok)
    _install_route(monkeypatch, _act)
    assert await h.poll_once() == 1
    assert fetcher.marked == ["uid3"]
    assert h.dedup.was_seen("<m3@x>") is True


# --- D28: an outage is not "no new mail" ------------------------------------

@pytest.mark.asyncio
async def test_a_fetch_outage_records_a_durable_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("VERDICTS_DB_PATH", os.path.join(str(tmp_path), "verdicts.db"))
    import core.credential_verdicts as cv
    cv._reset_for_tests()
    fetcher = _Fetcher([], boom=MailFetchError("login rejected", auth=True,
                                               key="imap.x:me@x"))
    h = _harness(tmp_path, fetcher, router=None)
    assert await h.poll_once() == 0
    v = cv.verdict("imap", "imap.x:me@x")
    assert v is not None and v.count == 1
    cv._reset_for_tests()
