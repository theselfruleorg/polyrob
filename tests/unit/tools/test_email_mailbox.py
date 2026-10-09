"""The agent's mailbox verbs (tools/email_mailbox.py) — 2026-10-04.

A goal stopped because the email tool could only SEND: the activation link sat in
the agent's own inbox and it could not read it. These tests pin the read verbs,
the threaded reply/forward through the ONE gated send rail, and the rules that
keep the verbs from disturbing the inbound surface (own connection, read-only
select, BODY.PEEK, delete = move to Trash). No network: a fake IMAP connection.
"""
import os
import tempfile
import types
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from tools.email_mailbox import (
    EmailDeleteAction, EmailFoldersAction, EmailForwardAction, EmailListAction,
    EmailMarkAction, EmailReadAction, EmailReplyAction, EmailSaveAttachmentAction,
    extract_links, imap_quote, parse_list_line, prefixed_subject, reply_recipients,
    safe_filename, search_criteria,
)
from tools.email_tool import EmailSendAction, EmailTool


def _raw_activation() -> bytes:
    msg = MIMEMultipart("mixed")
    msg["From"] = "Ethereum Magicians <noreply@ethereum-magicians.org>"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "[Ethereum Magicians] Activate your account"
    msg["Date"] = "Sat, 04 Oct 2026 08:00:00 +0000"
    msg["Message-ID"] = "<act-1@ethereum-magicians.org>"
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Click https://ethereum-magicians.org/u/activate-account/abc123 to activate.",
                        "plain"))
    alt.attach(MIMEText('<p><a href="https://ethereum-magicians.org/u/activate-account/abc123">'
                        'Activate</a></p>', "html"))
    msg.attach(alt)
    att = MIMEApplication(b"%PDF-1.4 fake", _subtype="pdf")
    att.add_header("Content-Disposition", "attachment", filename="terms.pdf")
    msg.attach(att)
    return msg.as_bytes()


class _FakeIMAP:
    """Records every command; serves one folder with one message (uid 42)."""

    def __init__(self, raw=None, caps=("IMAP4REV1", "MOVE")):
        self.raw = raw or _raw_activation()
        self.capabilities = caps
        self.calls = []
        self.selected = None

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        self.selected = (folder, readonly)
        return "OK", [b"1"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
                      b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [b"40 42"]
        if command == "FETCH":
            if "HEADER.FIELDS" in args[1]:
                hdr = (b"From: Ethereum Magicians <noreply@ethereum-magicians.org>\r\n"
                       b"Subject: Activate\r\nDate: Sat, 04 Oct 2026\r\n\r\n")
                return "OK", [(b"1 (UID 42 FLAGS () BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {80}", hdr),
                              b")",
                              (b"2 (UID 40 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {80}", hdr),
                              b")"]
            assert args[1] == "(BODY.PEEK[])", "a read must never set \\Seen"
            if args[0] != "42":
                return "OK", [None]
            return "OK", [(b"1 (UID 42 BODY[] {999}", self.raw), b")"]
        return "OK", [b"done"]

    def logout(self):
        self.calls.append(("logout",))


def _tool(container=None, conn=None):
    cfg = types.SimpleNamespace(
        gmail_email="bot@example.com", gmail_app_password="app-pw",
        gmail_smtp_server="smtp.test", gmail_smtp_port=587, gmail_imap_server="imap.test")
    tool = EmailTool(name="email", config=cfg, container=None)
    tool._initialized = True
    tool._enabled = True
    if container is not None:
        tool.container = container
    tool._mbox_conn = conn if conn is not None else _FakeIMAP()
    return tool


class _FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


class _FakeContainer:
    def __init__(self, allowlist=None):
        self._allowlist = allowlist

    def get_service(self, name):
        return self._allowlist if name == "outbound_allowlist" else None


def _al():
    from core.surfaces.outbound_allowlist import OutboundAllowlist
    return OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))


class _Ctx:
    def __init__(self, session_id="s1"):
        self.user_id = "rob"
        self.session_id = session_id
        self.is_sub_agent = False
        self.role = "orchestrator"
        self.metadata = {}


# --- pure helpers -------------------------------------------------------------

def test_imap_quote_escapes_and_drops_line_breaks():
    assert imap_quote('a"b\\c\r\nX') == '"a\\"b\\\\c X"'


def test_parse_list_line_reads_flags_and_quoted_name():
    assert parse_list_line(b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"') == \
        ("[Gmail]/Trash", ["\\HasNoChildren", "\\Trash"])


def test_extract_links_reads_href_and_text_once():
    links = extract_links("see https://a.test/x.", '<a href="https://a.test/x">x</a>'
                                                   '<a href="https://b.test/y?a=1&amp;b=2">y</a>')
    assert links == ["https://a.test/x", "https://b.test/y?a=1&b=2"]


def test_reply_recipients_prefers_reply_to_and_never_self():
    msg = {"from": "A <a@x>", "reply_to": "r@x", "to": "bot@example.com, c@x", "cc": "d@x"}
    assert reply_recipients(msg, "bot@example.com", False) == (["r@x"], [])
    assert reply_recipients(msg, "bot@example.com", True) == (["r@x"], ["c@x", "d@x"])


def test_subject_prefix_does_not_stack():
    assert prefixed_subject("Re: hi", "Re:") == "Re: hi"
    assert prefixed_subject("hi", "Fwd:") == "Fwd: hi"


def test_safe_filename_strips_directories():
    assert safe_filename("../../etc/passwd", "f") == "passwd"
    assert safe_filename("", "fallback") == "fallback"


def test_search_criteria_quotes_values():
    assert search_criteria(unread_only=True, from_addr='x"y') == ["UNSEEN", "FROM", '"x\\"y"']
    assert search_criteria() == ["ALL"]


# --- read verbs ---------------------------------------------------------------

def test_every_mailbox_verb_is_registered():
    tool = _tool(container=_FakeContainer())
    names = set(tool.get_actions())
    assert {"email_list", "email_read", "email_save_attachment", "email_reply",
            "email_forward", "email_folders", "email_mark", "email_move",
            "email_delete", "email_send"} <= names


@pytest.mark.asyncio
async def test_list_is_newest_first_readonly_and_never_touches_the_surface_socket():
    conn = _FakeIMAP()
    tool = _tool(conn=conn)
    sentinel = object()
    tool.imap_connection = sentinel
    res = await tool.email_list(EmailListAction(folder="[Gmail]/Spam"))
    assert res.error is None
    assert res.extracted_content.index("uid 42") < res.extracted_content.index("uid 40")
    assert "uid 42" in res.extracted_content and "[unread]" in res.extracted_content
    assert ("select", '"[Gmail]/Spam"', True) in conn.calls
    assert not any(c[0] == "uid" and c[1] == "STORE" for c in conn.calls)
    assert tool.imap_connection is sentinel


@pytest.mark.asyncio
async def test_read_returns_body_links_and_attachments():
    tool = _tool()
    res = await tool.email_read(EmailReadAction(uid="42"))
    assert res.error is None
    out = res.extracted_content
    assert "Activate your account" in out
    assert "- https://ethereum-magicians.org/u/activate-account/abc123" in out
    assert "1. terms.pdf · application/pdf" in out


@pytest.mark.asyncio
async def test_read_of_a_missing_uid_is_an_error_not_an_empty_mail():
    res = await _tool().email_read(EmailReadAction(uid="7"))
    assert res.error and "no message with uid 7" in res.error


@pytest.mark.asyncio
async def test_read_refuses_a_non_numeric_imap_uid():
    res = await _tool().email_read(EmailReadAction(uid="1:*"))
    assert res.error


@pytest.mark.asyncio
async def test_save_attachment_writes_into_the_session_workspace(monkeypatch):
    ws = tempfile.mkdtemp()
    monkeypatch.setattr("tools.controller.message_send._resolve_session_workspace",
                        lambda sid, uid: ws)
    res = await _tool().email_save_attachment(
        EmailSaveAttachmentAction(uid="42", index=1), execution_context=_Ctx())
    assert res.error is None
    path = os.path.join(ws, "email_attachments", "terms.pdf")
    with open(path, "rb") as f:
        assert f.read() == b"%PDF-1.4 fake"


# --- send verbs (the ONE gated rail) --------------------------------------------

@pytest.mark.asyncio
async def test_reply_to_a_denied_sender_is_refused_with_the_allow_command(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_reply(EmailReplyAction(uid="42", body="thanks"), execution_context=_Ctx())
    assert smtp.sent == []
    assert "/allow email noreply@ethereum-magicians.org" in res.error


@pytest.mark.asyncio
async def test_reply_to_an_allowed_sender_is_threaded(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    al = _al()
    al.allow("rob", "email", "noreply@ethereum-magicians.org", note="test")
    tool = _tool(container=_FakeContainer(allowlist=al))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_reply(EmailReplyAction(uid="42", body="done"), execution_context=_Ctx())
    assert res.error is None, res.error
    sent = smtp.sent[0]
    assert sent["Subject"] == "Re: [Ethereum Magicians] Activate your account"
    assert sent["In-Reply-To"] == "<act-1@ethereum-magicians.org>"
    assert sent["To"] == "noreply@ethereum-magicians.org"


@pytest.mark.asyncio
async def test_forward_to_owner_carries_the_attachment(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_forward(EmailForwardAction(uid="42", to="owner@example.com", note="fyi"),
                                   execution_context=_Ctx())
    assert res.error is None, res.error
    sent = smtp.sent[0]
    assert sent["Subject"].startswith("Fwd: ")
    names = [p.get_filename() for p in sent.walk() if p.get_filename()]
    assert names == ["terms.pdf"]


@pytest.mark.asyncio
async def test_send_with_one_denied_cc_sends_nothing(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_send(EmailSendAction(to="owner@example.com", subject="s", body="b",
                                                cc=["stranger@x.com"]), execution_context=_Ctx())
    assert smtp.sent == []
    assert "/allow email stranger@x.com" in res.error


@pytest.mark.asyncio
async def test_send_attachment_outside_the_workspace_is_refused(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    ws = tempfile.mkdtemp()
    monkeypatch.setattr("tools.controller.message_send._resolve_session_workspace",
                        lambda sid, uid: ws)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_send(EmailSendAction(to="owner@example.com", subject="s", body="b",
                                                attachments=["/etc/hosts"]), execution_context=_Ctx())
    assert smtp.sent == []
    assert "attachment rejected" in res.error


@pytest.mark.asyncio
async def test_send_html_cc_and_workspace_attachment_to_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "report.txt"), "w") as f:
        f.write("numbers")
    monkeypatch.setattr("tools.controller.message_send._resolve_session_workspace",
                        lambda sid, uid: ws)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    tool.smtp_connection = smtp = _FakeSMTP()
    res = await tool.email_send(EmailSendAction(
        to="owner@example.com", subject="s", body="b", html="<b>b</b>",
        cc=["owner@example.com"], attachments=[os.path.join(ws, "report.txt")]),
        execution_context=_Ctx())
    assert res.error is None, res.error
    sent = smtp.sent[0]
    assert [p.get_filename() for p in sent.walk() if p.get_filename()] == ["report.txt"]
    assert any(p.get_content_type() == "text/html" for p in sent.walk())
    assert "attached: report.txt" in res.extracted_content


# --- organisation -------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_moves_to_the_special_use_trash(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    conn = _FakeIMAP()
    res = await _tool(conn=conn).email_delete(EmailDeleteAction(uid="42"), execution_context=types.SimpleNamespace(user_id="owner", role="orchestrator", metadata={}))
    assert res.error is None
    assert ("uid", "MOVE", "42", '"[Gmail]/Trash"') in conn.calls
    assert not any(c[0] == "uid" and c[1] == "EXPUNGE" for c in conn.calls)


@pytest.mark.asyncio
async def test_mark_read_selects_writable_and_stores_seen(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    conn = _FakeIMAP()
    res = await _tool(conn=conn).email_mark(EmailMarkAction(uid="42", read=True), execution_context=types.SimpleNamespace(user_id="owner", role="orchestrator", metadata={}))
    assert res.error is None
    assert ("select", '"INBOX"', False) in conn.calls
    assert ("uid", "STORE", "42", "+FLAGS", "(\\Seen)") in conn.calls


@pytest.mark.asyncio
async def test_folders_lists_special_use_marks():
    res = await _tool().email_folders(EmailFoldersAction())
    assert "- [Gmail]/Trash  (\\Trash)" in res.extracted_content
    assert "- INBOX" in res.extracted_content


@pytest.mark.asyncio
async def test_agentmail_has_no_folders(monkeypatch):
    tool = _tool()
    tool.provider = "agentmail"
    res = await tool.email_folders(EmailFoldersAction())
    assert res.error and "agentmail" in res.error


def test_refusal_taint_gate_sees_a_non_owner_cc(monkeypatch):
    from tools.controller import refusal_taint_gate as g
    monkeypatch.setattr(g, "_is_owner", lambda c, u, s, t: t == "owner@example.com")
    assert g.reaches_non_owner(None, "email_send", {"to": "owner@example.com"}, "rob") is False
    assert g.reaches_non_owner(None, "email_send",
                               {"to": "owner@example.com", "cc": ["x@y.z"]}, "rob") is True
    assert g.reaches_non_owner(None, "email_reply", {"uid": "1"}, "rob") is True


@pytest.mark.asyncio
async def test_list_reads_flags_that_follow_the_header_literal():
    class _TrailingFlags(_FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH" and "HEADER.FIELDS" in args[1]:
                hdr = b"From: a@x\r\nSubject: s\r\n\r\n"
                return "OK", [(b"1 (UID 42 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}", hdr),
                              b" FLAGS (\\Seen))"]
            if command == "SEARCH":
                return "OK", [b"42"]
            return super().uid(command, *args)
    res = await _tool(conn=_TrailingFlags()).email_list(EmailListAction())
    assert "uid 42" in res.extracted_content and "[unread]" not in res.extracted_content


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,params", [
    ("email_mark", EmailMarkAction(uid="42", read=True)),
    ("email_delete", EmailDeleteAction(uid="42")),
])
async def test_mailbox_mutations_refuse_untrusted_callers(verb, params, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    for context in (None, types.SimpleNamespace(user_id="stranger", role="orchestrator", metadata={}),
                    types.SimpleNamespace(user_id="owner", role="orchestrator", metadata={"turn_kind": "self_wake"})):
        conn = _FakeIMAP()
        result = await getattr(_tool(conn=conn), verb)(params, execution_context=context)
        assert result.error
        assert not conn.calls


def test_reply_subject_cannot_inject_decoded_header_lines():
    from tools.email_mailbox import prefixed_subject
    assert prefixed_subject("hello\r\nBcc: x@example.com", "Re:") == "Re: hello Bcc: x@example.com"


@pytest.mark.asyncio
async def test_agentmail_read_accepts_a_gmail_message_id():
    """An AgentMail uid is the RFC Message-ID; Gmail's carry ``+`` and ``=``.
    The uid check refused them, so email_read/reply/forward/save could not reach
    mail a Gmail user sent to the agent's managed inbox."""
    mid = "<CAJx+Ab=cD_e-F@mail.gmail.com>"

    class _AM:
        address = "rob@agentmail.to"

        async def get_message(self, message_id):
            assert message_id == mid
            return {"message_id": mid, "from": "a@gmail.com", "subject": "hi",
                    "text": "see https://x.example/a", "timestamp": "t"}

    tool = _tool()
    tool.provider = "agentmail"
    tool.agentmail = _AM()

    async def _noop():
        return None
    tool.ensure_imap = _noop
    res = await tool.email_read(EmailReadAction(uid=mid))
    assert res.error is None, res.error
    assert "https://x.example/a" in res.extracted_content
    # A path-changing character is still refused before any request.
    bad = await tool.email_read(EmailReadAction(uid="<a/../b@x>"))
    assert bad.error == "email_read: invalid uid"
