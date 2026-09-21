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
from email.header import decode_header
import os
import asyncio

from pydantic import BaseModel, ConfigDict, Field

from core.config import BotConfig
from core.exceptions import ConfigurationError, APIError, AuthenticationError, ToolError
from tools.base_tool import BaseTool, ToolStatus
from tools.controller.types import ActionResult


# Memory of rejected SMTP logins. 057 WS-F moved it into the DURABLE core-tier
# verdict store: the backoff now survives a restart and is shared by the three
# service units, instead of each process re-discovering the same 535. The TTL
# constant lives in the store (it was a magic 900 duplicated across a layering
# boundary); this name is kept because it is the tool's published contract.
from core.credential_verdicts import SMTP_TTL_SEC as SMTP_AUTH_BACKOFF_SEC  # noqa: E402


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
    """Send an email. Only the owner's email or an owner-allowlisted address is
    permitted as `to`; other targets are denied (mirrors the `message` action's
    tier gate — see tools/controller/message_send.py)."""
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., description="Recipient email address.")
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)

class EmailTool(BaseTool):
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

    async def _connect_imap(self) -> None:
        """Establish IMAP connection."""
        try:
            # Connect to IMAP server
            self.imap_connection = imaplib.IMAP4_SSL(self.imap_server)

            # Login
            self.imap_connection.login(self.config.gmail_email, self.config.gmail_app_password)

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
                references=references)

        try:
            # Create message. With attachments the structure is
            # multipart/mixed( multipart/alternative(text[, html]), attachment... )
            # so plain-body/HTML-alternative semantics are preserved for MUAs that
            # render the alternative part but ignore attachments. Without
            # attachments, keep the original flat multipart/alternative shape
            # byte-identical to preserve today's behaviour.
            if attachments:
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

            if attachments:
                msg.attach(body_part)
                for path in attachments:
                    try:
                        self._attach_file(msg, path)
                    except Exception as e:
                        self.logger.warning(f"send_email: skipping unreadable attachment '{path}': {e}")

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
        "Send an email to a specific address. Only the owner's email or an "
        "owner-allowlisted address is permitted; other targets are denied.",
        param_model=EmailSendAction,
    )
    async def email_send(self, params: EmailSendAction, execution_context=None) -> ActionResult:
        """Agent-callable send, gated the same way as the generic `message` action
        (tools/controller/message_send.py): resolve owner/allowlisted/open/denied
        tier, apply the open-tier daily-send cap, seed a correspondent binding
        before sending, then send via SMTP directly (no MessageRouter hop needed
        — this tool owns its own SMTP connection). On a first-contact open-tier
        send, reports it (telemetry + owner notice) after the send succeeds."""
        import os as _os

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

        tier = resolve_target_tier(surface="email", target=params.to, user_id=user_id,
                                   allowlist=allowlist, owner_targets=owner_targets,
                                   policy=policy, domains=domains)
        if tier == "denied":
            return ActionResult(
                error=("target not on owner allowlist; ask the owner to run "
                       f"`polyrob owner allow email {params.to}`"),
                include_in_memory=True)

        # D9 (2026-09-21 interface audit): the 031 owner pause, the SAME probe
        # `perform_message_send` applies. This escape-hatch send had none, so
        # `/pause pings`, `/pause social` and even `/pause all` left an
        # autonomous goal or cron session free to keep emailing. The gate is
        # BEFORE the cap/seed/send rail, so a paused send creates no
        # correspondent binding and burns no cap slot either.
        from tools.controller.message_send import message_pause_refusal
        pause_refusal = message_pause_refusal(execution_context, None, tier=tier)
        if pause_refusal is not None:
            self.logger.info("email_send refused by owner pause: %s (%s)",
                             params.to, pause_refusal)
            return ActionResult(error=pause_refusal, include_in_memory=True)

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
        body = scrub_secret_shapes(params.body or "")
        subject = scrub_secret_shapes(params.subject or "")
        if body != (params.body or "") or subject != (params.subject or ""):
            self.logger.warning("email_send: redacted a secret shape before delivery")

        # 2026-08-29: this escape-hatch send bypassed the same owner-resend
        # cooldown the generic `message` tool enforces (tools/controller/
        # action_registration.py::message), so a completion-judge retry could
        # (and did, observed 2026-08-28 22:19Z) deliver the owner the
        # same report twice within minutes. Mirror that gate here.
        try:
            from tools.controller.turn_origin import _autonomous_owner_resend_cooldown_refusal
            cooldown_refusal = _autonomous_owner_resend_cooldown_refusal(
                execution_context, None, container=self.container, user_id=user_id,
                surface="email", target=params.to, owner_targets=owner_targets,
                # The BODY alone — `store.record_outbound` below writes exactly
                # this, and the gate compares content hashes. Passing
                # subject+body made the hashes unmatchable, which does not
                # loosen the gate, it kills it while it still looks present.
                text=body)
            if cooldown_refusal is not None:
                return cooldown_refusal
        except Exception:
            self.logger.debug("email_send owner cooldown check skipped (fail-open)", exc_info=True)

        session_id = getattr(execution_context, "session_id", None) or ""

        store = None
        if tier != "owner" and self.container is not None:
            try:
                store = self.container.get_service("conversation_store")
            except Exception:
                store = None

        # T6: the open-tier (incl. a domains-match) daily send is capped
        # tenant+surface-wide, checked BEFORE the seed rail.
        if tier == "open" and store is not None:
            cap = resolve_outbound_daily_cap(user_id, home_dir=home_dir)
            try:
                sent_today = store.outbound_count_surface_since(user_id, "email", 86400)
            except Exception:
                sent_today = 0  # fail-open: a query fault must never block the send
            if sent_today >= cap:
                return ActionResult(
                    error=(f"outbound daily send cap ({cap}) reached for email; "
                           "owner can raise outbound.daily_send_cap"),
                    include_in_memory=True)

        # T6: first-contact MUST be detected before the send (see
        # tools/controller/message_send.py for why the seed state alone can't
        # tell new-vs-existing).
        first_contact = False
        if store is not None and tier != "owner":
            try:
                first_contact = store.get(user_id, "email", params.to) is None
            except Exception:
                first_contact = False

        if tier != "owner" and self.container is not None:
            try:
                from core.surfaces.seed import maybe_seed_correspondent
                seed_state = maybe_seed_correspondent(
                    self.container, surface="email", address=params.to,
                    session_id=session_id, user_id=user_id, provenance="owner")
            except Exception as e:  # fail-soft: a seed fault must not block the send
                self.logger.debug(f"email_send correspondent seed skipped: {e}")
                seed_state = None
            if seed_state == "refused":
                return ActionResult(
                    error=("correspondent per-day cap reached — reply binding "
                           "refused; email not sent"),
                    include_in_memory=True)

        try:
            message_id = await self.send_email_ex(params.to, subject, body)
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
                    store.record_outbound(user_id, "email", params.to, body,
                                          mid=(str(message_id) if message_id else None),
                                          subject=subject, session_id=session_id)
            except Exception as e:
                self.logger.warning("email_send conversation record skipped for %s: "
                                    "%s — this outbound is missing from the "
                                    "transcript", params.to, e, exc_info=True)
            if message_id and tier != "owner":
                self._seed_thread_anchor(params.to, str(message_id), user_id,
                                         session_id)

        # T6: first-contact report — AFTER a successful send+record.
        # Only report for open-tier sends (allowlisted/supervised sends to known
        # correspondents are NOT "open contact" and should not fire this report).
        if first_contact and tier == "open":
            await notify_first_contact(self.container, user_id, session_id, "email", params.to)

        # 057 WS-E: the per-rail proof rule rides WITH the receipt, from the ONE
        # table (core/rails/verification.py). Fail-open to "": an unavailable
        # table must never turn a SUCCESSFUL send into an error.
        try:
            from core.rails.verification import verification_line
            _proof = verification_line("email", message_id=message_id)
        except Exception:
            _proof = ""
        return ActionResult(
            extracted_content=(f"email[{tier}] -> {params.to} OK "
                               f"(message-id {message_id})"
                               + (f"\n{_proof}" if _proof else "")),
            include_in_memory=True)

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

            # Select folder
            self.imap_connection.select(folder)
            
            # Build search criteria
            search_criteria = []
            if unread_only:
                search_criteria.append('UNSEEN')
            if since_date:
                date_str = since_date.strftime("%d-%b-%Y")
                search_criteria.append(f'SINCE "{date_str}"')
                
            # Perform search
            if search_criteria:
                _, message_numbers = self.imap_connection.search(None, ' '.join(search_criteria))
            else:
                _, message_numbers = self.imap_connection.search(None, 'ALL')
                
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
                    _, msg_data = self.imap_connection.fetch(num, '(BODY.PEEK[])')
                    email_body = msg_data[0][1]
                    email_message = email.message_from_bytes(email_body)
                    
                    # Decode subject
                    subject = decode_header(email_message["Subject"])[0]
                    if isinstance(subject[0], bytes):
                        subject = subject[0].decode(subject[1] or 'utf-8')
                    else:
                        subject = subject[0]
                        
                    # Get sender
                    from_header = decode_header(email_message["From"])[0]
                    if isinstance(from_header[0], bytes):
                        from_addr = from_header[0].decode(from_header[1] or 'utf-8')
                    else:
                        from_addr = from_header[0]
                        
                    # Get date
                    date_str = email_message["Date"]
                    
                    # Get content
                    content = ""
                    html_content = ""
                    
                    if email_message.is_multipart():
                        for part in email_message.walk():
                            if part.get_content_type() == "text/plain":
                                content = part.get_payload(decode=True).decode()
                            elif part.get_content_type() == "text/html":
                                html_content = part.get_payload(decode=True).decode()
                    else:
                        content = email_message.get_payload(decode=True).decode()
                        
                    emails.append({
                        'id': num.decode(),
                        'subject': subject,
                        'from': from_addr,
                        'date': date_str,
                        'content': content,
                        'html_content': html_content
                    })
                    
                except Exception as e:
                    self.logger.error(f"Error processing email {num}: {str(e)}")
                    continue
                    
            return emails

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
                
            self.imap_connection.select(folder)
            self.imap_connection.store(message_id.encode(), '+FLAGS', '\\Seen')
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
                
            self.imap_connection.select(folder)
            self.imap_connection.store(message_id.encode(), '+FLAGS', '\\Deleted')
            self.imap_connection.expunge()
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to delete email: {str(e)}")
            raise APIError(f"Failed to delete email: {str(e)}")

    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize() 