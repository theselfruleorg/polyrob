import logging
from typing import Dict, Any, Optional, List, Union
import smtplib
import ssl
import mimetypes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from datetime import datetime
import imaplib
import email
import os
import asyncio

from pydantic import BaseModel, ConfigDict, Field

from core.config import BotConfig
from core.exceptions import ConfigurationError, APIError, AuthenticationError, ToolError
from tools.base_tool import BaseTool, ToolStatus
from tools.controller.types import ActionResult
from tools.email_providers.mime import _decode, auto_headers, is_auto_generated


# Memory of rejected SMTP logins. 057 WS-F moved it into the DURABLE core-tier
# verdict store: the backoff now survives a restart and is shared by the three
# service units, instead of each process re-discovering the same 535. The TTL
# constant lives in the store (it was a magic 900 duplicated across a layering
# boundary); this name is kept because it is the tool's published contract.
from core.credential_verdicts import SMTP_TTL_SEC as SMTP_AUTH_BACKOFF_SEC  # noqa: E402

#: Seconds one IMAP socket operation may block before it fails (EM1). Without
#: it a half-open server held the read forever.
IMAP_TIMEOUT_S = 30


def smtp_verdict_key(server: Any, port: Any, user: Any, secret: Any = None) -> str:
    """The ONE verdict key for an SMTP rail: ``server:port:user``, plus a
    ``#<digest>`` of the password when one is given. The digest is a change
    detector (`core.credential_verdicts.credential_digest`, never a reveal): a
    verdict belongs to the credential that earned it, so a NEW app password has
    no standing verdict and is probed on the first start after the fix instead
    of waiting out a hold that now grows to six hours."""
    base = f"{server}:{port}:{user or ''}"
    if secret:
        from core.credential_verdicts import credential_digest
        return f"{base}#{credential_digest(secret)}"
    return base


class _SmtpAuthFailures:
    """Back-compat dict view over the verdict store, keyed ``(server, port, user)``.

    ``_SMTP_AUTH_FAILURES`` used to be a real dict; it is kept as a delegator so
    the historical call/test shape (``get``/``pop``/``[k] =``/``clear``/truthiness)
    keeps working over the durable store. It is NOT a full Mapping — nothing ever
    iterated it.
    """

    @staticmethod
    def _key(key) -> str:
        return smtp_verdict_key(*key) if isinstance(key, tuple) else str(key)

    def get(self, key, default=None):
        from core.credential_verdicts import verdict
        v = verdict("smtp", self._key(key))
        return default if v is None else v.last_seen

    def pop(self, key, default=None):
        from core.credential_verdicts import clear_rejection
        prev = self.get(key, default)
        clear_rejection("smtp", self._key(key))
        return prev

    def __setitem__(self, key, _value) -> None:
        from core.credential_verdicts import record_rejection
        record_rejection("smtp", self._key(key), code="535")

    def __contains__(self, key) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        from core.credential_verdicts import active
        return len(active("smtp"))

    def __bool__(self) -> bool:
        return len(self) > 0

    def clear(self) -> None:
        from core.credential_verdicts import clear_kind
        clear_kind("smtp")


_SMTP_AUTH_FAILURES = _SmtpAuthFailures()


def smtp_credentials_rejected() -> bool:
    """056 WS4: True while ANY SMTP login was rejected (535) within the backoff
    window. Delegates to the core-tier register the autonomous toolset reads."""
    from core.credential_verdicts import rejected_within
    return rejected_within("smtp", SMTP_AUTH_BACKOFF_SEC)

class EmailSendAction(BaseModel):
    """Send an email. EVERY recipient (to, cc and bcc) must pass the same tier
    gate as the `message` action: the owner's email, an owner-allowlisted
    address, or a capped first contact under an open/domains outbound policy
    (see tools/controller/message_send.py). One denied address refuses the send."""
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., description="Recipient email address. Several: separate with commas.")
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1, description="Plain-text body.")
    cc: List[str] = Field(default_factory=list, description="Cc addresses.")
    bcc: List[str] = Field(default_factory=list, description="Bcc addresses.")
    html: Optional[str] = Field(default=None, description="Optional HTML version of the body.")
    attachments: List[str] = Field(
        default_factory=list,
        description="Files to attach: paths inside this session's workspace.")

class EmailReadMachineMailAction(BaseModel):
    """Read the agent's own machine-sent mail (activation links, verification codes)."""
    model_config = ConfigDict(extra="forbid")
    sender_contains: Optional[str] = Field(
        None, description="Only mail whose From contains this text (e.g. 'magicians').")
    limit: int = Field(10, ge=1, le=500, description="How many recent messages to scan.")


# Machine senders only: a human's mail reaches the agent through the gated email
# SURFACE (owner / allowlisted / correspondent tiers), never through this read.
_MACHINE_SENDER_MARKERS = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
                           "notifications", "notification", "mailer")
_READ_SCAN_CAP = 50
_READ_BODY_CAP = 2000


def _is_machine_sender(from_header: str) -> bool:
    addr = from_header.rsplit("<", 1)[-1].rstrip(">").strip().lower()
    local = addr.split("@", 1)[0]
    return any(m in local for m in _MACHINE_SENDER_MARKERS)


def split_addresses(value: Union[str, List[str], None]) -> List[str]:
    """``"a@x, b@y"`` / ``["a@x"]`` / None -> a clean, de-duplicated address list."""
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace(";", ",").split(",")
    out: List[str] = []
    for item in items:
        addr = str(item or "").strip()
        if addr and addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    return out


#: Tier order for a multi-recipient send: the MOST open recipient decides the
#: pause/cap rules, so adding the owner on cc never loosens a stranger send.
_TIER_RANK = {"owner": 0, "allowlisted": 1, "open": 2}

from tools.email_mailbox import EmailMailboxMixin  # noqa: E402


class EmailTool(EmailMailboxMixin, BaseTool):
    """Service for handling email communication."""
    
    # Default email server settings
    DEFAULT_SMTP_SERVER = 'smtp.gmail.com'
    DEFAULT_SMTP_PORT = 587
    DEFAULT_IMAP_SERVER = 'imap.gmail.com'

    # Provider seam defaults as CLASS attributes so partially-constructed tools
    # (tests build via object.__new__) resolve to the legacy smtp path.
    provider = "smtp"
    agentmail = None
    # 057 WS-F: the IMAP half can be ready while SMTP is refused (class attribute
    # so a partially-constructed tool resolves it too).
    _imap_ready = False
    
    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management'  # Only need rate limiting for API calls
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {}  # No optional services needed

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize email service."""
        super().__init__(name=name, config=config, container=container)

        # Initialize email settings
        self.smtp_server = getattr(config, 'gmail_smtp_server', self.DEFAULT_SMTP_SERVER)
        self.smtp_port = getattr(config, 'gmail_smtp_port', self.DEFAULT_SMTP_PORT)
        self.imap_server = getattr(config, 'gmail_imap_server', self.DEFAULT_IMAP_SERVER)

        # Initialize connections
        self.smtp_connection = None
        self.imap_connection = None

        # Provider seam (2026-08-18): smtp (legacy, byte-identical) | agentmail
        # (managed HTTP inbox — the agent's OWN address, provisioned at init).
        from core.config_policy.policy import email_provider
        self.provider = email_provider()
        self.agentmail = None
        if self.provider == "agentmail":
            import os as _os
            from tools.email_providers.agentmail import AgentMailClient
            self.agentmail = AgentMailClient(_os.environ.get("AGENTMAIL_API_KEY", ""))

    # --- 057 WS-F: the two halves of an email rail are independent -------------
    # A rejected SMTP login must not stop INBOUND mail. Before WS-F the email
    # surface called ``ensure_initialized()`` on every 60 s poll, which ran the
    # SMTP probe, which failed, which logged an ERROR — 1,439 lines/day on prod —
    # and dragged IMAP polling down with a send-side credential problem.

    def _smtp_key(self) -> str:
        """This rail's verdict key — server, port, user AND the password digest."""
        return smtp_verdict_key(self.smtp_server, self.smtp_port,
                                self.config.gmail_email, self.config.gmail_app_password)

    def _smtp_verdict(self):
        """The live SMTP auth verdict for THIS rail, or None."""
        try:
            from core.credential_verdicts import verdict
            v = verdict("smtp", self._smtp_key())
            return v if (v is not None and v.live) else None
        except Exception:
            return None

    def _smtp_verdict_refusal(self) -> Optional[str]:
        """The refusal text for a live verdict — no network probe, with the remedy."""
        v = self._smtp_verdict()
        if v is None:
            return None
        from core.credential_verdicts import duration_text, since_text
        return (
            f"SMTP connection test failed: login rejected since {since_text(v)} "
            f"({v.code or '535'}, bad credentials; {v.count} rejection(s)); in backoff "
            f"for another {duration_text(v.remaining_sec)} — fix the app password to "
            f"clear it (a changed password is probed on the next start)")

    def _warn_smtp_verdict_once(self, refusal: str) -> None:
        """One WARNING per process per outage, instead of an ERROR per call."""
        v = self._smtp_verdict()
        try:
            from core.credential_verdicts import warn_once
            fresh = warn_once("smtp", self._smtp_key(),
                              episode=(v.first_seen if v else None))
        except Exception:
            fresh = True
        if fresh:
            self.logger.warning("%s", refusal)
        else:
            self.logger.debug("%s", refusal)

    async def initialize(self) -> None:
        """Full init (SMTP included). Refuses QUIETLY while an auth verdict is live.

        The refusal happens BEFORE ``BaseTool.initialize`` so a dead SMTP rail
        costs one WARNING per process, not one ERROR per caller (the email
        surface, every session start, every cron delivery).
        """
        if not self._initialized and self.provider == "smtp":
            refusal = self._smtp_verdict_refusal()
            if refusal:
                self._warn_smtp_verdict_once(refusal)
                self._status = ToolStatus.FAILED
                self._error_message = refusal
                raise ToolError(refusal)
        await super().initialize()

    async def ensure_smtp(self) -> None:
        """Ensure the SEND half is usable. Same meaning as ``ensure_initialized``."""
        await self.ensure_initialized()

    async def ensure_imap(self) -> None:
        """Ensure the RECEIVE half is usable — NEVER probes SMTP.

        Inbound mail needs credentials and a connection, not a send handshake,
        so a 535 on the send side leaves polling alone.
        """
        if self._initialized or self._imap_ready:
            return
        if self.provider == "agentmail":
            await self.ensure_initialized()
            return
        if not all([self.config.gmail_email, self.config.gmail_app_password]):
            raise ConfigurationError("Email credentials not configured")
        self._imap_ready = True

    async def _initialize(self) -> None:
        """Initialize email service."""
        try:
            if self.provider == "agentmail":
                # Managed inbox: ensure the agent's own address exists. No SMTP
                # creds are needed or checked on this path.
                from core.instance import resolve_instance_id
                await self.agentmail.provision(resolve_instance_id())
                return

            # Validate credentials
            if not all([self.config.gmail_email, self.config.gmail_app_password]):
                self._status = ToolStatus.FAILED
                self._error_message = "Email credentials not configured"
                raise ConfigurationError(self._error_message)

            # Test SMTP connection
            try:
                await self._test_smtp_connection()
            except Exception as e:
                self._status = ToolStatus.FAILED
                self._error_message = f"Failed to connect to SMTP server: {e}"
                raise ToolError(self._error_message)

        except Exception as e:
            self._status = ToolStatus.FAILED
            self._error_message = str(e)
            raise ToolError(f"Failed to initialize email service: {e}")

    async def _test_smtp_connection(self) -> None:
        """Test SMTP connection.

        A rejected login (535) is remembered in the DURABLE verdict store for
        ``SMTP_AUTH_BACKOFF_SEC`` (057 WS-F): the tool still fails closed, but
        from memory — and now ACROSS restarts and across the three service units
        that share the data dir — instead of re-sending bad credentials to the
        provider on every surface poll and every session start (5,071 rejected
        Gmail logins in 24 h, 2026-09-18). Transient errors are never cached; a
        success clears the verdict.
        """
        key = self._smtp_key()
        refusal = self._smtp_verdict_refusal()
        if refusal:
            self._warn_smtp_verdict_once(refusal)
            raise ToolError(refusal)
        try:
            context = ssl.create_default_context()
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls(context=context)
            server.login(self.config.gmail_email, self.config.gmail_app_password)
            server.quit()
            try:
                # A working login means the RAIL is up: drop every smtp verdict,
                # including one an old (since-replaced) password earned.
                from core.credential_verdicts import clear_kind
                clear_kind("smtp")
            except Exception:
                pass
            self.logger.info("SMTP connection test successful")
        except smtplib.SMTPAuthenticationError as e:
            fresh = True
            try:
                from core.credential_verdicts import record_rejection
                fresh = record_rejection(
                    "smtp", key, code="535",
                    remedy=("fix the app password (GMAIL_APP_PASSWORD) or switch "
                            "EMAIL_PROVIDER=agentmail")).count == 1
            except Exception:
                pass
            # 056 WS4/WS9: a durable, layering-safe fact for the status snapshot
            # (core cannot import tools) — rendered as a health WARN with the
            # remedy. 057 WS-F: emitted once per OUTAGE (a fresh verdict), not
            # once per rejected login — 105 identical events in 24 h said nothing
            # the first one did not.
            if fresh:
                from core.event_log import emit
                emit("email_auth_rejected", source="email", attrs={
                    "server": str(self.smtp_server),
                    "account": str(self.config.gmail_email or "")[:3] + "…",
                    "code": "535"})
            raise ToolError(f"SMTP connection test failed: {e}")
        except Exception as e:
            raise ToolError(f"SMTP connection test failed: {e}")

    async def _cleanup(self) -> None:
        """Cleanup email service resources."""
        try:
            # Close SMTP connection
            if self.smtp_connection:
                self.smtp_connection.quit()
                self.smtp_connection = None

            # Close IMAP connection
            if self.imap_connection:
                self.imap_connection.logout()
                self.imap_connection = None

            # Close the mailbox verbs' own IMAP connection (tools/email_mailbox.py)
            if self._mbox_conn is not None:
                try:
                    self._mbox_conn.logout()
                except Exception:
                    pass
                self._mbox_conn = None

            # Close the managed-inbox HTTP client
            if self.agentmail is not None:
                await self.agentmail.aclose()

            self.logger.info("Email service cleaned up successfully")

        except Exception as e:
            self.logger.error(f"Error during email service cleanup: {e}")
            raise ToolError(f"Failed to cleanup email service: {e}")

    async def _connect_smtp(self) -> None:
        """Establish SMTP connection."""
        try:
            # Create SSL context
            context = ssl.create_default_context()
            
            # Connect to SMTP server
            self.smtp_connection = smtplib.SMTP(self.smtp_server, self.smtp_port)
            self.smtp_connection.starttls(context=context)
            
            # Login
            self.smtp_connection.login(self.config.gmail_email, self.config.gmail_app_password)
            
        except smtplib.SMTPAuthenticationError as e:
            # D30: the LIVE send path rejected the credential. Only the
            # `test_connection` probe used to write a verdict, so on a box whose
            # first SMTP contact is a real send (every headless deploy) the 535
            # was re-discovered on every send, no status seat could name it, and
            # `effective_autonomous_tools` never dropped the email tool.
            self._record_smtp_rejection(e)
            raise AuthenticationError(f"SMTP authentication failed: {str(e)}")
        except Exception as e:
            raise APIError(f"SMTP connection failed: {str(e)}")

    def _record_smtp_rejection(self, exc: BaseException) -> None:
        """Write the durable 535 verdict for THIS rail. Fail-open."""
        try:
            from core.credential_verdicts import record_rejection
            v = record_rejection(
                "smtp", self._smtp_key(), code="535",
                remedy=("fix the app password (GMAIL_APP_PASSWORD) or switch "
                        "EMAIL_PROVIDER=agentmail"))
            if v.count == 1:
                from core.event_log import emit
                emit("email_auth_rejected", source="email", attrs={
                    "server": str(self.smtp_server),
                    "account": str(self.config.gmail_email or "")[:3] + "…",
                    "code": "535"})
        except Exception:
            self.logger.warning("SMTP rejection verdict not recorded (%s) — the "
                                "outage will not show on a status surface", exc,
                                exc_info=True)

    async def run_imap(self, fn, *args):
        """Run one blocking ``imaplib`` step OFF the event loop (EM1).

        ``imaplib`` is synchronous: called inside ``async def`` it held the
        gateway's ONE loop for as long as the server took, and a half-open
        socket had no timeout at all. The step runs in a worker thread, one
        at a time per tool (an ``imaplib`` connection is not thread-safe), and
        the socket carries :data:`IMAP_TIMEOUT_S`. Do not nest two calls.
        """
        loop = asyncio.get_running_loop()
        held = getattr(self, "_imap_lock_pair", None)
        if held is None or held[0] is not loop:
            held = (loop, asyncio.Lock())
            self._imap_lock_pair = held
        async with held[1]:
            return await asyncio.to_thread(fn, *args)

    async def _connect_imap(self) -> None:
        """Establish IMAP connection (off the loop, with a socket timeout)."""
        user, password = self.config.gmail_email, self.config.gmail_app_password

        def _open():
            conn = imaplib.IMAP4_SSL(self.imap_server, timeout=IMAP_TIMEOUT_S,
                                   ssl_context=ssl.create_default_context())
            try:
                conn.login(user, password)
            except BaseException:
                try:
                    conn.logout()
                except Exception:
                    pass
                raise
            return conn

        try:
            self.imap_connection = await self.run_imap(_open)
        except imaplib.IMAP4.error as e:
            raise AuthenticationError(f"IMAP authentication failed: {str(e)}")
        except Exception as e:
            raise APIError(f"IMAP connection failed: {str(e)}")

    def _attach_file(self, outer_msg: MIMEMultipart, path: str) -> None:
        """Attach a local file to `outer_msg` as `Content-Disposition: attachment`.
        Images use MIMEImage (correct subtype from the guessed content-type / the
        file's magic bytes); everything else is a generic MIMEBase + base64 payload.
        Raises on a missing/unreadable file — callers skip+log per attachment so one
        bad path never loses the rest of the email (Task 7)."""
        filename = os.path.basename(path)
        ctype, encoding = mimetypes.guess_type(path)
        if ctype and encoding is None:
            maintype, subtype = ctype.split('/', 1)
        else:
            maintype, subtype = 'application', 'octet-stream'
        with open(path, 'rb') as f:
            data = f.read()
        if maintype == 'image':
            part = MIMEImage(data, _subtype=subtype)
        else:
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment', filename=filename)
        outer_msg.attach(part)

    @staticmethod
    def _attach_bytes(outer_msg: MIMEMultipart, part: Dict[str, Any]) -> None:
        """Attach an in-memory ``{filename, mime, data}`` part (a forwarded file)."""
        data = part.get("data")
        if not data:
            return
        maintype, _, subtype = str(part.get("mime") or "application/octet-stream").partition("/")
        mime = MIMEBase(maintype or "application", subtype or "octet-stream")
        mime.set_payload(data)
        encoders.encode_base64(mime)
        mime.add_header('Content-Disposition', 'attachment',
                        filename=str(part.get("filename") or "attachment"))
        outer_msg.attach(mime)

    async def send_email(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> bool:
        """Send an email (legacy bool contract). Delegates to :meth:`send_email_ex`.

        Returns:
            bool: True if email was sent successfully

        Raises:
            APIError: If sending fails
        """
        return bool(await self.send_email_ex(
            to_email, subject, body, html=html, cc=cc, bcc=bcc,
            attachments=attachments))

    async def send_email_ex(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        *,
        html: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        extra_files: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Send an email, returning the minted RFC 5322 Message-ID (A3, 2026-07-13).

        Mints its own ``Message-ID`` (domain taken from the From address) so the
        caller can bind the outbound to a correspondent thread anchor — a reply's
        ``In-Reply-To`` then exact-matches in the registry. ``in_reply_to``/
        ``references`` set the standard threading headers so OUR replies land in
        the correspondent's thread too.

        Args:
            to_email: Recipient email address(es)
            subject: Email subject
            body: Plain text email body
            html: Optional HTML version of the email body
            cc: Optional CC recipient(s)
            bcc: Optional BCC recipient(s)
            attachments: Optional list of local file paths to attach (Task 7). A path
                that can't be read is skipped with a logged WARN — the email still
                sends with whatever attachments succeeded (never loses the body).
            in_reply_to: Message-ID of the mail this replies to (sets In-Reply-To).
            references: References header value (defaults to in_reply_to when unset).
            extra_files: in-memory parts ``{filename, mime, data}`` (a forward
                carries the original mail's attachments this way).

        Returns:
            str: the Message-ID stamped on the sent mail

        Raises:
            APIError: If sending fails
        """
        await self.ensure_smtp()

        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        if self.provider == "agentmail":
            # Managed inbox: HTTP send from the agent's own address. The client
            # mints + returns the RFC Message-ID (same thread-anchor contract).
            return await self.agentmail.send(
                to_email, subject, body, html=html, cc=cc, bcc=bcc,
                attachments=attachments, in_reply_to=in_reply_to,
                references=references, extra_parts=extra_files)

        try:
            # Create message. With attachments the structure is
            # multipart/mixed( multipart/alternative(text[, html]), attachment... )
            # so plain-body/HTML-alternative semantics are preserved for MUAs that
            # render the alternative part but ignore attachments. Without
            # attachments, keep the original flat multipart/alternative shape
            # byte-identical to preserve today's behaviour.
            if attachments or extra_files:
                msg = MIMEMultipart('mixed')
                body_part = MIMEMultipart('alternative')
            else:
                msg = MIMEMultipart('alternative')
                body_part = msg

            msg['From'] = self.config.gmail_email
            msg['Subject'] = subject

            # A3: mint our own Message-ID (domain from the From address) so the
            # thread anchor can be recorded; SMTP servers keep an existing header.
            from email.utils import make_msgid
            try:
                _domain = (self.config.gmail_email or "").split("@", 1)[1] or None
            except IndexError:
                _domain = None
            message_id = make_msgid(domain=_domain) if _domain else make_msgid()
            msg['Message-ID'] = message_id
            if in_reply_to:
                msg['In-Reply-To'] = in_reply_to
                msg['References'] = references or in_reply_to
            elif references:
                msg['References'] = references

            # Handle multiple recipients
            if isinstance(to_email, list):
                msg['To'] = ', '.join(to_email)
            else:
                msg['To'] = to_email

            # Add CC if provided
            if cc:
                if isinstance(cc, list):
                    msg['Cc'] = ', '.join(cc)
                else:
                    msg['Cc'] = cc

            # Add BCC if provided
            if bcc:
                if isinstance(bcc, list):
                    msg['Bcc'] = ', '.join(bcc)
                else:
                    msg['Bcc'] = bcc

            # Add text body
            body_part.attach(MIMEText(body, 'plain'))

            # Add HTML version if provided
            if html:
                body_part.attach(MIMEText(html, 'html'))

            if attachments or extra_files:
                msg.attach(body_part)
                for path in attachments or []:
                    try:
                        self._attach_file(msg, path)
                    except Exception as e:
                        self.logger.warning(f"send_email: skipping unreadable attachment '{path}': {e}")
                for part in extra_files or []:
                    self._attach_bytes(msg, part)

            # Get all recipients
            all_recipients = []
            if isinstance(to_email, list):
                all_recipients.extend(to_email)
            else:
                all_recipients.append(to_email)
                
            if cc:
                if isinstance(cc, list):
                    all_recipients.extend(cc)
                else:
                    all_recipients.append(cc)
                    
            if bcc:
                if isinstance(bcc, list):
                    all_recipients.extend(bcc)
                else:
                    all_recipients.append(bcc)

            # Send email. A previously-live connection can go stale (e.g. the
            # server closes it after a timeout) without smtp_connection becoming
            # None, so `send_message` on it raises instead of triggering a
            # reconnect. Retry once against a freshly-established connection
            # rather than leaving the tool wedged until process restart.
            if not self.smtp_connection:
                await self._connect_smtp()

            try:
                self.smtp_connection.send_message(msg)
            except (smtplib.SMTPException, OSError) as e:
                self.logger.warning(
                    f"send_email_ex: SMTP send failed on existing connection ({e}), "
                    "reconnecting and retrying once"
                )
                self.smtp_connection = None
                await self._connect_smtp()
                self.smtp_connection.send_message(msg)

            self.logger.info(f"Email sent successfully to {msg['To']}")
            return message_id

        except Exception as e:
            self.logger.error(f"Failed to send email: {str(e)}")
            raise APIError(f"Failed to send email: {str(e)}")

    @BaseTool.action(
        "Send an email from the agent's own mailbox: your subject, a plain body, "
        "optional html, cc/bcc and workspace-file attachments. To answer a mail you "
        "received use email_reply (it keeps the thread); to pass one on use "
        "email_forward. EVERY recipient must be allowed: the owner's email, an "
        "owner-allowlisted address, or (when the owner set an open/domains outbound "
        "policy) a capped first contact; anything else is refused, and only the "
        "owner can allow it (/allow email <address>). An autonomous run obeys the "
        "same gate as `message`.",
        param_model=EmailSendAction,
    )
    async def email_send(self, params: EmailSendAction, execution_context=None) -> ActionResult:
        """Agent-callable send. All gating lives in :meth:`_gated_send`."""
        return await self._gated_send(
            execution_context, action="email_send",
            to=split_addresses(params.to), cc=split_addresses(params.cc),
            bcc=split_addresses(params.bcc), subject=params.subject, body=params.body,
            html=params.html, attachments=list(params.attachments or []))

    async def _gated_send(self, execution_context, *, action: str, to: List[str],
                          subject: str, body: str, cc: Optional[List[str]] = None,
                          bcc: Optional[List[str]] = None, html: Optional[str] = None,
                          attachments: Optional[List[str]] = None,
                          in_reply_to: Optional[str] = None,
                          references: Optional[str] = None,
                          extra_files: Optional[List[Dict[str, Any]]] = None) -> ActionResult:
        """The ONE gated outbound rail for every email verb (send/reply/forward),
        gated the same way as the generic `message` action
        (tools/controller/message_send.py): the forged/autonomous gate, the
        owner/allowlisted/open/denied tier of EVERY recipient, the owner pause,
        the secret scrub, the owner-resend cooldown, the open-tier daily cap,
        a correspondent seed before sending, then the send, the transcript record
        and the thread anchor. On a first-contact open-tier send, reports it
        (telemetry + owner notice) after the send succeeds.

        ``extra_files`` are in-memory parts (``{filename, mime, data}``) that a
        forward carries from the original mail; they never touch the disk."""
        import os as _os

        cc, bcc = list(cc or []), list(bcc or [])
        recipients = split_addresses(list(to) + cc + bcc)
        if not recipients:
            return ActionResult(error=f"{action}: no recipient", include_in_memory=True)

        # Review E3: the SAME forged/autonomous gate as the `message` action
        # (tools/controller/turn_origin.py) — this tool is the second outbound
        # rail and had none. It falls through exactly where `message` does
        # (MESSAGE_AUTONOMOUS_ALLOWLISTED, ON under AUTONOMY_MODE=autonomous; an
        # open/domains outbound policy), so an owner-configured autonomous mail
        # rail keeps working and the tier gate below still decides the target.
        from tools.controller.turn_origin import _autonomous_message_refusal
        refusal = _autonomous_message_refusal(execution_context, None)
        if refusal is not None:
            return ActionResult(
                error=(refusal.extracted_content or "").replace("message:", f"{action}:", 1),
                include_in_memory=True)

        from core.instance import resolve_owner_email
        from core.surfaces.outbound_policy import (
            notify_first_contact, resolve_outbound_daily_cap, resolve_outbound_policy,
        )
        from core.surfaces.outbound_target import resolve_target_tier

        user_id = getattr(execution_context, "user_id", None) or ""
        if not user_id:
            from core.identity import resolve_identity
            user_id = resolve_identity()

        allowlist = self.container.get_service("outbound_allowlist") if self.container else None
        owner_targets = {}
        owner_email = resolve_owner_email(_os.environ)
        if owner_email:
            owner_targets["email"] = owner_email

        # D19: the IDENTITY axis — `core.runtime_paths.prefs_home_dir()`, the ONE
        # home every preference WRITER resolves. This used to read the container's
        # `config.data_dir`, which on a server is `<data_home>/data`: a shadow no
        # seat ever writes to, so the owner could set `outbound.policy` or
        # `outbound.daily_send_cap` from any surface and this path kept reading
        # the env default. Same fix as `message_send._pref_home_dir` (C10).
        from core.runtime_paths import prefs_home_dir
        home_dir = prefs_home_dir() if self.container is not None else None
        policy, domains = resolve_outbound_policy(user_id, "email", home_dir=home_dir)

        tiers = {addr: resolve_target_tier(surface="email", target=addr, user_id=user_id,
                                           allowlist=allowlist, owner_targets=owner_targets,
                                           policy=policy, domains=domains)
                 for addr in recipients}
        denied = [a for a, t in tiers.items() if t == "denied"]
        if denied:
            return ActionResult(
                error=("target not on owner allowlist; only the owner can allow "
                       "it: " + "; ".join(f"/allow email {a}" for a in denied)),
                include_in_memory=True)
        tier = max(tiers.values(), key=lambda t: _TIER_RANK.get(t, 2))
        non_owner = [a for a, t in tiers.items() if t != "owner"]
        open_targets = [a for a, t in tiers.items() if t == "open"]

        # D9 (2026-09-21 interface audit): the 031 owner pause, the SAME probe
        # `perform_message_send` applies. This escape-hatch send had none, so
        # `/pause pings`, `/pause social` and even `/pause all` left an
        # autonomous goal or cron session free to keep emailing. The gate is
        # BEFORE the cap/seed/send rail, so a paused send creates no
        # correspondent binding and burns no cap slot either.
        from tools.controller.message_send import message_pause_refusal
        pause_refusal = message_pause_refusal(execution_context, None, tier=tier)
        if pause_refusal is not None:
            self.logger.info("%s refused by owner pause: %s (%s)",
                             action, ", ".join(recipients), pause_refusal)
            return ActionResult(error=pause_refusal, include_in_memory=True)

        # Attachments are confined to the session workspace and screened BEFORE
        # anything is recorded — an arbitrary path is an exfiltration rail.
        session_id = getattr(execution_context, "session_id", None) or ""
        files, err = self._screen_attachments(list(attachments or []), session_id, user_id)
        if err:
            return ActionResult(error=f"{action}: attachment rejected: {err}",
                                include_in_memory=True)

        # D66: scrub secret SHAPES out of the body and subject before anything
        # else looks at them. `MessageRouter.publish` does this for every routed
        # send; this tool owns its own SMTP connection and bypassed the router,
        # so it was the ONE outbound path that could mail a key out verbatim.
        #
        # ⚠️ Scrubbed HERE, above the cooldown gate, so `body` is the ONE string
        # the gate hashes, the conversation store records and SMTP sends. Two
        # spellings of the body make the hashes unmatchable, which does not
        # loosen the gate — it kills it while it still looks present.
        from core.secret_scrub import scrub_secret_shapes
        raw_body, raw_subject, raw_html = body or "", subject or "", html or ""
        body = scrub_secret_shapes(raw_body)
        subject = scrub_secret_shapes(raw_subject)
        html = scrub_secret_shapes(raw_html) or None
        if body != raw_body or subject != raw_subject or (html or "") != raw_html:
            self.logger.warning("%s: redacted a secret shape before delivery", action)

        # 2026-08-29: this escape-hatch send bypassed the same owner-resend
        # cooldown the generic `message` tool enforces (tools/controller/
        # action_registration.py::message), so a completion-judge retry could
        # (and did, observed 2026-08-28 22:19Z) deliver the owner the
        # same report twice within minutes. Mirror that gate here.
        try:
            from tools.controller.turn_origin import _autonomous_owner_resend_cooldown_refusal
            for addr in recipients:
                cooldown_refusal = _autonomous_owner_resend_cooldown_refusal(
                    execution_context, None, container=self.container, user_id=user_id,
                    surface="email", target=addr, owner_targets=owner_targets,
                    # The BODY alone — `store.record_outbound` below writes exactly
                    # this, and the gate compares content hashes. Passing
                    # subject+body made the hashes unmatchable, which does not
                    # loosen the gate, it kills it while it still looks present.
                    text=body)
                if cooldown_refusal is not None:
                    return cooldown_refusal
        except Exception:
            self.logger.debug("%s owner cooldown check skipped (fail-open)", action,
                              exc_info=True)

        store = None
        if non_owner and self.container is not None:
            try:
                store = self.container.get_service("conversation_store")
            except Exception:
                store = None

        # T6: the open-tier (incl. a domains-match) daily send is capped
        # tenant+surface-wide, checked BEFORE the seed rail. Each open-tier
        # recipient is one send against the cap.
        if open_targets and store is not None:
            cap = resolve_outbound_daily_cap(user_id, home_dir=home_dir)
            try:
                sent_today = store.outbound_count_surface_since(user_id, "email", 86400)
            except Exception:
                sent_today = 0  # fail-open: a query fault must never block the send
            if sent_today + len(open_targets) > cap:
                return ActionResult(
                    error=(f"outbound daily send cap ({cap}) reached for email; "
                           "owner can raise outbound.daily_send_cap"),
                    include_in_memory=True)

        # T6: first-contact MUST be detected before the send (see
        # tools/controller/message_send.py for why the seed state alone can't
        # tell new-vs-existing).
        first_contacts: List[str] = []
        if store is not None:
            for addr in open_targets:
                try:
                    if store.get(user_id, "email", addr) is None:
                        first_contacts.append(addr)
                except Exception:
                    pass

        if self.container is not None:
            for addr in non_owner:
                try:
                    from core.surfaces.seed import maybe_seed_correspondent
                    seed_state = maybe_seed_correspondent(
                        self.container, surface="email", address=addr,
                        session_id=session_id, user_id=user_id, provenance="owner")
                except Exception as e:  # fail-soft: a seed fault must not block the send
                    self.logger.debug(f"{action} correspondent seed skipped: {e}")
                    seed_state = None
                if seed_state == "refused":
                    return ActionResult(
                        error=("correspondent per-day cap reached — reply binding "
                               "refused; email not sent"),
                        include_in_memory=True)

        try:
            message_id = await self.send_email_ex(
                to if len(to) != 1 else to[0], subject, body, html=html,
                cc=cc or None, bcc=bcc or None, attachments=files or None,
                in_reply_to=in_reply_to, references=references,
                extra_files=extra_files or None)
        except Exception as e:
            return ActionResult(error=f"send failed: {e}", include_in_memory=True)

        if self.container is not None:
            try:
                if store is None:
                    store = self.container.get_service("conversation_store")
                if store is not None:
                    # D31: the minted Message-ID was DROPPED here, so the
                    # transcript could not be threaded and a reply's
                    # In-Reply-To had nothing to match.
                    for addr in recipients:
                        store.record_outbound(user_id, "email", addr, body,
                                              mid=(str(message_id) if message_id else None),
                                              subject=subject, session_id=session_id)
            except Exception as e:
                self.logger.warning("%s conversation record skipped for %s: "
                                    "%s — this outbound is missing from the "
                                    "transcript", action, recipients, e, exc_info=True)
            if message_id:
                for addr in non_owner:
                    self._seed_thread_anchor(addr, str(message_id), user_id, session_id)

        # T6: first-contact report — AFTER a successful send+record.
        # Only report for open-tier sends (allowlisted/supervised sends to known
        # correspondents are NOT "open contact" and should not fire this report).
        for addr in first_contacts:
            await notify_first_contact(self.container, user_id, session_id, "email", addr)

        # 057 WS-E: the per-rail proof rule rides WITH the receipt, from the ONE
        # table (core/rails/verification.py). Fail-open to "": an unavailable
        # table must never turn a SUCCESSFUL send into an error.
        try:
            from core.rails.verification import verification_line
            _proof = verification_line("email", message_id=message_id)
        except Exception:
            _proof = ""
        attached = [os.path.basename(f) for f in files] + [
            str(f.get("filename") or "attachment") for f in (extra_files or [])]
        return ActionResult(
            extracted_content=(f"email[{tier}] -> {', '.join(recipients)} OK "
                               f"(message-id {message_id})"
                               + (f"; attached: {', '.join(attached)}" if attached else "")
                               + (f"\n{_proof}" if _proof else "")),
            include_in_memory=True)

    def _screen_attachments(self, paths: List[str], session_id: str,
                            user_id: str) -> "tuple[List[str], Optional[str]]":
        """Workspace-confined, screened attachment paths — the SAME contract as
        ``message(media_paths=…)`` (core/surfaces/attachments.py): inside the
        session workspace, size cap, secret filename/content screen, injection
        scan. Returns ``(real_paths, error)``."""
        if not paths:
            return [], None
        from core.surfaces.attachments import (
            message_media_max_mb, screen_attachment_path, validate_media_paths,
        )
        from tools.controller.message_send import _resolve_session_workspace
        validated, err = validate_media_paths(
            paths, _resolve_session_workspace(session_id, user_id))
        if err:
            return [], err
        try:
            from modules.memory.task.threat_scan import is_suspicious as _scanner
        except ImportError:
            _scanner = None
        for real in validated:
            reason = screen_attachment_path(real, max_mb=message_media_max_mb(),
                                            scanner=_scanner)
            if reason:
                return [], f"{os.path.basename(real)}: {reason}"
        return list(validated), None

    def _seed_thread_anchor(self, address: str, mid: str, user_id: str,
                            session_id: str) -> None:
        """Bind this outbound Message-ID to the sending session (D31).

        Without the anchor a reply's ``In-Reply-To`` resolves against nothing,
        so a second session talking to the same address is ambiguous and the
        reply is quarantined. Fail-soft: never affects a completed send.
        """
        try:
            registry = (self.container.get_service("correspondent_registry")
                        if self.container else None)
            if registry is None or not hasattr(registry, "seed_thread_anchor"):
                return
            registry.seed_thread_anchor(
                surface="email", address=address, thread_id=mid,
                session_id=session_id, user_id=user_id)
        except Exception as e:
            self.logger.warning("email_send thread-anchor seed skipped for %s: %s "
                                "— a reply may not route back", address, e,
                                exc_info=True)

    @BaseTool.action(
        "Read your OWN machine-sent mail: account activation links and verification "
        "codes from no-reply or auto-generated senders, so you can finish a sign-up yourself. Ordinary "
        "correspondents' mail is not returned here (it reaches you through the normal email "
        "channel; email_list/email_read reach any mail). "
        "Read-only: nothing is marked read and nothing is answered.",
        param_model=EmailReadMachineMailAction,
    )
    async def email_read_machine_mail(self, params: EmailReadMachineMailAction,
                                      execution_context=None) -> ActionResult:
        """Prod 2026-10-04: an authorized sign-up's activation mail was dropped by the
        surface (auto-generated: never answered) and the agent had no way to read it.
        Reads with the non-consuming `read_emails` (BODY.PEEK) and filters to machine
        mail — a no-reply sender, or what the surface's `is_auto_generated` drops —
        so the surface's UNSEEN queue and the human-mail gate are untouched."""
        if not isinstance(params, EmailReadMachineMailAction):
            from pydantic import TypeAdapter
            params = TypeAdapter(EmailReadMachineMailAction).validate_python(params)
        try:
            mails = await self.read_emails(limit=min(params.limit, _READ_SCAN_CAP),
                                           unread_only=False)
        except Exception as e:
            return ActionResult(error=f"Could not read the mailbox: {e}", include_in_memory=True)
        needle = (params.sender_contains or "").lower()
        rows = []
        for m in reversed(mails or []):  # newest first
            frm = str(m.get("from") or "")
            # 0009: exactly what the surface refuses to answer (headers, ONE
            # classifier) plus the no-reply sender names.
            machine = _is_machine_sender(frm) or is_auto_generated(m)
            if not machine or (needle and needle not in frm.lower()):
                continue
            body = (m.get("content") or m.get("html_content") or "")[:_READ_BODY_CAP]
            rows.append(f"From: {frm}\nDate: {m.get('date', '')}\n"
                        f"Subject: {m.get('subject', '')}\n\n{body}")
        if not rows:
            return ActionResult(
                extracted_content="No machine-sent mail matched in the recent mailbox.",
                include_in_memory=True)
        return ActionResult(
            extracted_content=f"📬 {len(rows)} machine-sent mail(s), newest first:\n\n"
                              + "\n\n---\n\n".join(rows),
            include_in_memory=True)

    async def read_emails(
        self,
        folder: str = 'INBOX',
        limit: int = 10,
        unread_only: bool = False,
        since_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Read emails from specified folder.
        
        Args:
            folder: Email folder to read from
            limit: Maximum number of emails to return
            unread_only: Only return unread emails
            since_date: Only return emails since this date
            
        Returns:
            List of email dictionaries containing metadata and content
            
        Raises:
            APIError: If reading fails
        """
        await self.ensure_imap()   # 057 WS-F: inbound never waits on the SMTP probe

        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        if self.provider == "agentmail":
            return await self._read_emails_agentmail(limit=limit)

        try:
            if not self.imap_connection:
                await self._connect_imap()
            conn = self.imap_connection

            def _read_sync():
                # EM1: the whole select → search → fetch runs in a worker thread.
                conn.select(folder)
            
                # Build search criteria
                search_criteria = []
                if unread_only:
                    search_criteria.append('UNSEEN')
                if since_date:
                    date_str = since_date.strftime("%d-%b-%Y")
                    search_criteria.append(f'SINCE "{date_str}"')
                
                # Perform search
                if search_criteria:
                    _, message_numbers = conn.search(None, ' '.join(search_criteria))
                else:
                    _, message_numbers = conn.search(None, 'ALL')
                
                # Get message numbers and limit results
                message_nums = message_numbers[0].split()
                if limit:
                    message_nums = message_nums[-limit:]
                
                emails = []
                for num in message_nums:
                    try:
                        # D4 (same rule as `surfaces/email/fetchers.py`): a READ
                        # must not consume the mailbox. `(RFC822)` sets `\Seen` as
                        # a side effect of the FETCH, and the email SURFACE's
                        # inbound queue IS the UNSEEN set — so one agent-side
                        # `read_emails` marked every waiting message read and the
                        # surface never routed it. `BODY.PEEK[]` returns the same
                        # bytes and touches no flag; `mark_as_read` stays the ONE
                        # place a message is marked.
                        #
                        # The server answers `BODY.PEEK[]` with a `BODY[]` tag, but
                        # imaplib's literal parse is positional — `msg_data[0][1]`
                        # is the raw message either way.
                        _, msg_data = conn.fetch(num, '(BODY.PEEK[])')
                        email_body = msg_data[0][1]
                        email_message = email.message_from_bytes(email_body)
                    
                        # Decode subject
                        # Every chunk, any charset (an 8-bit or unknown one used
                        # to raise here and skip the whole mail).
                        subject = _decode(email_message["Subject"])
                        
                        # Get sender
                        # Every chunk: the first alone drops the address of an
                        # encoded display name (`=?utf-8?q?Name?= <a@b>`).
                        from_addr = _decode(email_message["From"])
                        
                        # Get date
                        date_str = email_message["Date"]
                    
                        # Get content
                        content = ""
                        html_content = ""
                    
                        def _text(part) -> str:
                            # The part's OWN charset: a bare .decode() raised on a
                            # latin-1/cp1252 body and the whole mail was skipped.
                            payload = part.get_payload(decode=True) or b""
                            try:
                                return payload.decode(part.get_content_charset() or "utf-8",
                                                      errors="replace")
                            except LookupError:  # an unknown charset name
                                return payload.decode("utf-8", errors="replace")

                        if email_message.is_multipart():
                            for part in email_message.walk():
                                if part.get_content_type() == "text/plain":
                                    content = _text(part)
                                elif part.get_content_type() == "text/html":
                                    html_content = _text(part)
                        else:
                            content = _text(email_message)
                        
                        emails.append({
                            'id': num.decode(),
                            'subject': subject,
                            'from': from_addr,
                            'date': date_str,
                            'content': content,
                            'html_content': html_content,
                            # OS5 markers: `email_read_machine_mail` selects with
                            # the surface's own `is_auto_generated` (0009).
                            'headers': auto_headers(email_message),
                        })
                    
                    except Exception as e:
                        self.logger.error(f"Error processing email {num}: {str(e)}")
                        continue
                    
                return emails

            return await self.run_imap(_read_sync)

        except Exception as e:
            self.logger.error(f"Failed to read emails: {str(e)}")
            raise APIError(f"Failed to read emails: {str(e)}")

    async def _read_emails_agentmail(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Map the managed-inbox message shape onto read_emails' legacy dicts.

        ``extracted_text`` (reply content without quoted history) is preferred
        for ``content``; the raw ``text`` is the fallback.
        """
        summaries = await self.agentmail.list_messages(limit=limit)
        emails: List[Dict[str, Any]] = []
        for summary in summaries:
            mid = summary.get("message_id")
            if not mid:
                continue
            try:
                full = await self.agentmail.get_message(mid)
            except Exception as e:
                self.logger.error(f"Error processing email {mid}: {e}")
                continue
            # D26: the managed inbox dropped attachments entirely, so a read of
            # an AgentMail message looked like a mail with nothing attached.
            # ONE normalizer with the IMAP path (`{filename, mime, data}`); an
            # entry whose bytes the provider did not include keeps `data=None`
            # and is reported as unreadable rather than vanishing (D64).
            from tools.email_providers.agentmail import normalize_agentmail_attachments
            emails.append({
                'id': mid,
                'subject': full.get('subject') or '',
                'from': full.get('from') or '',
                'date': full.get('timestamp') or '',
                'content': full.get('extracted_text') or full.get('text') or '',
                'html_content': full.get('html') or '',
                'attachments': normalize_agentmail_attachments(
                    full.get('attachments')),
                'headers': (full.get('headers')
                            if isinstance(full.get('headers'), dict) else {}),
            })
        return emails

    async def mark_as_read(self, message_id: str, folder: str = 'INBOX') -> bool:
        """Mark an email as read.
        
        Args:
            message_id: Email message ID
            folder: Folder containing the email
            
        Returns:
            bool: True if successful
            
        Raises:
            APIError: If operation fails
        """
        await self.ensure_imap()   # 057 WS-F: inbound never waits on the SMTP probe
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            if not self.imap_connection:
                await self._connect_imap()
            conn = self.imap_connection

            def _mark_sync():
                conn.select(folder)
                conn.store(message_id.encode(), '+FLAGS', '\\Seen')

            await self.run_imap(_mark_sync)
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to mark email as read: {str(e)}")
            raise APIError(f"Failed to mark email as read: {str(e)}")

    async def delete_email(self, message_id: str, folder: str = 'INBOX') -> bool:
        """Delete an email.
        
        Args:
            message_id: Email message ID
            folder: Folder containing the email
            
        Returns:
            bool: True if successful
            
        Raises:
            APIError: If deletion fails
        """
        await self.ensure_imap()   # 057 WS-F: inbound never waits on the SMTP probe
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            if not self.imap_connection:
                await self._connect_imap()
            conn = self.imap_connection

            def _delete_sync():
                conn.select(folder)
                conn.store(message_id.encode(), '+FLAGS', '\\Deleted')
                conn.expunge()

            await self.run_imap(_delete_sync)
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to delete email: {str(e)}")
            raise APIError(f"Failed to delete email: {str(e)}")

    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize()
