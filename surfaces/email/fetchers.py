"""MailFetcher seam — transport-specific "fetch new mail" behind one protocol.

Extracted from EmailHarness (Task 5, 2026-08-18 agent-mail plan) so the poll
loop's dedup/route/act body is transport-blind:

- ``ImapFetcher`` — the legacy IMAP behaviour, byte-identical: UNSEEN search on
  hardcoded INBOX, ``\\Seen`` as the durable handled-mark.
- ``AgentMailFetcher`` — the managed HTTP inbox. No provider read-state is
  needed: the ``MessageDedup`` store IS the durable handled-mark (read-only
  ``was_seen`` probe here; the recording ``seen`` check stays at route time in
  ``process_email``), so ``mark_handled`` is a no-op.

Both yield ``(handle, normalized_dict)`` pairs where the dict is exactly the
``normalize_email_message`` shape ``process_email`` consumes.
"""
from __future__ import annotations

import logging
from typing import Any, List, Protocol, Tuple

from surfaces.email.dedup import MessageDedup
# The AgentMail wire shape is owned by its CLIENT (tools tier). Imported, never
# re-derived — and imported DOWNWARD, which is what the layering ratchet allows
# (`surfaces` may read `tools`; `tools` may not read `surfaces`).
from tools.email_providers.agentmail import normalize_agentmail_attachments

logger = logging.getLogger(__name__)


class MailFetchError(Exception):
    """The mailbox could not be read.

    D28 (2026-09-21 interface audit): both fetchers answered an OUTAGE with
    ``[]``, which is byte-identical to "no new mail". A dead IMAP login, a
    revoked API key and an empty inbox all rendered as silence, so the owner's
    only signal that inbound mail had stopped was that nobody replied.

    ``auth`` marks the subset the owner must fix (a rejected credential);
    the harness records a durable verdict for those so every status seat can
    name the outage with a SINCE clock.
    """

    def __init__(self, message: str, *, auth: bool = False, key: str = "") -> None:
        super().__init__(message)
        self.auth = auth
        self.key = key


class MailFetcher(Protocol):
    async def fetch_unread(self) -> List[Tuple[Any, dict]]:
        """New messages as (handle, normalized dict) pairs.

        Raises :class:`MailFetchError` when the mailbox could not be READ. An
        empty list means "read fine, nothing new" and nothing else.
        """
        ...

    def mark_handled(self, handle: Any) -> None:
        """Durably mark one fetched message as handled (idempotent, fail-open)."""
        ...


class ImapFetcher:
    """The legacy IMAP path, moved verbatim from EmailHarness."""

    def __init__(self, email_tool: Any) -> None:
        self.email_tool = email_tool

    async def fetch_unread(self) -> List[Tuple[Any, dict]]:
        import email as _email

        from surfaces.email.harness import normalize_email_message
        tool = self.email_tool
        # 057 WS-F: IMAP only. ``ensure_initialized()`` ran the SMTP probe, so a
        # rejected send-side login stopped INBOUND mail and logged an ERROR every
        # 60 s poll (1,439 lines/day on prod). Polling now needs the receive half.
        ensure = getattr(tool, "ensure_imap", None) or tool.ensure_initialized
        try:
            await ensure()
            if not getattr(tool, "imap_connection", None):
                await tool._connect_imap()
            conn = tool.imap_connection
            if conn is None:
                raise MailFetchError("IMAP connection unavailable after connect")
            conn.select("INBOX")
            _, nums = conn.search(None, "UNSEEN")
        except MailFetchError:
            raise
        except Exception as e:
            # D28: an outage is NOT "no new mail". Auth failures carry the flag
            # so the harness can record a durable verdict against the mailbox.
            raise MailFetchError(
                f"IMAP read failed: {type(e).__name__}: {e}",
                auth=self._is_auth_error(e), key=self._verdict_key()) from e
        out: List[Tuple[Any, dict]] = []
        for num in (nums[0].split() if nums and nums[0] else []):
            try:
                # D4: PEEK. ``(RFC822)`` sets ``\Seen`` as a side effect of the
                # FETCH itself, so a message was out of the UNSEEN set before
                # routing had a chance to fail — a crash between the two lost
                # the mail with no trace. ``BODY.PEEK[]`` returns the same bytes
                # and touches no flag; ``mark_handled`` remains the ONE place a
                # message is marked, and it now runs only on success.
                _, data = conn.fetch(num, "(BODY.PEEK[])")
                em = _email.message_from_bytes(data[0][1])
                out.append((num, normalize_email_message(em)))
            except Exception as e:
                logger.warning("email fetch %s failed: %s", num, e, exc_info=True)
        return out

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        """True when the mailbox refused our CREDENTIAL (owner action needed)."""
        from core.exceptions import AuthenticationError
        if isinstance(exc, AuthenticationError):
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(word in text for word in
                   ("authenticationfailed", "auth", "login", "invalid credentials"))

    def _verdict_key(self) -> str:
        """``server:user`` — the mailbox the verdict belongs to, never a secret."""
        tool = self.email_tool
        cfg = getattr(tool, "config", None)
        return f"{getattr(tool, 'imap_server', '?')}:{getattr(cfg, 'gmail_email', '') or ''}"

    def mark_handled(self, handle: Any) -> None:
        try:
            conn = getattr(self.email_tool, "imap_connection", None)
            if conn is not None and handle is not None:
                conn.store(handle, "+FLAGS", "\\Seen")
        except Exception as e:
            logger.debug("email mark-seen %s failed: %s", handle, e)


class AgentMailFetcher:
    """Managed-inbox path: list recent, filter handled, hydrate full messages."""

    def __init__(self, client: Any, dedup: MessageDedup, *, limit: int = 25) -> None:
        self.client = client
        self.dedup = dedup
        self.limit = limit

    async def fetch_unread(self) -> List[Tuple[Any, dict]]:
        # D29: the managed inbox must EXIST before it can be listed. Without
        # this the first poll after a restart raised "inbox not provisioned",
        # was swallowed, and inbound mail was dark until something else
        # happened to initialise the tool.
        await self._ensure_provisioned()
        try:
            summaries = await self.client.list_messages(limit=self.limit)
        except Exception as e:
            # D28: an outage is NOT "no new mail".
            raise MailFetchError(
                f"AgentMail list failed: {type(e).__name__}: {e}",
                auth=self._is_auth_error(e),
                key=self._verdict_key()) from e
        out: List[Tuple[Any, dict]] = []
        for summary in summaries:
            mid = summary.get("message_id")
            if not mid or self.dedup.was_seen(str(mid)):
                continue
            try:
                full = await self.client.get_message(mid)
            except Exception as e:
                logger.warning("agentmail get %s failed: %s", mid, e, exc_info=True)
                continue
            sender = str(full.get("from") or "")
            # D65: the agent's OWN address, decided by the ONE predicate. A
            # substring test called `bob@x.com` our own mail whenever the agent
            # was `b@x.com`, and missed a sub-addressed copy of our own address,
            # which `is_self_address` folds.
            if self._is_self(sender):
                # Our own outbound copy — never route the agent's mail back at it.
                continue
            in_reply_to = (full.get("in_reply_to") or "").strip()
            if not in_reply_to:
                # Thread-anchor synthesis: the provider may have rewritten our
                # outbound Message-ID header, so a reply can arrive without the
                # anchor the registry binds on. If we sent into this thread,
                # restore the minted mid (see tools/email_providers/agentmail.py).
                thread_id = full.get("thread_id")
                minted = (self.client.minted_mid_for_thread(str(thread_id))
                          if thread_id else None)
                if minted:
                    in_reply_to = minted
            out.append((mid, {
                "message_id": str(mid),
                "from": sender,
                "subject": str(full.get("subject") or ""),
                "body": str(full.get("extracted_text") or full.get("text") or ""),
                "in_reply_to": in_reply_to,
                "references": (full.get("references") or "").strip()
                              if isinstance(full.get("references"), str)
                              else " ".join(full.get("references") or []),
                # D26: the managed inbox dropped attachments entirely, so an
                # owner emailing a PDF over AgentMail got an answer written as
                # if the mail were empty — the exact failure the IMAP path was
                # fixed for on 2026-09-13.
                "attachments": normalize_agentmail_attachments(
                    full.get("attachments")),
            }))
        return out

    async def _ensure_provisioned(self) -> None:
        """Idempotently make sure the managed inbox exists. Fail LOUD."""
        if getattr(self.client, "inbox_id", None):
            return
        provision = getattr(self.client, "provision", None)
        if not callable(provision):
            # A client shape without provisioning (a fake, or a pre-provisioned
            # handle) answers for itself; `list_messages` reports its own fault.
            return
        try:
            from core.instance import resolve_instance_id
            await provision(resolve_instance_id())
        except MailFetchError:
            raise
        except Exception as e:
            raise MailFetchError(
                f"AgentMail inbox provisioning failed: {type(e).__name__}: {e}",
                auth=self._is_auth_error(e), key=self._verdict_key()) from e

    def _verdict_key(self) -> str:
        """The key EVERY ``imap``-kind verdict for this rail is written under.

        ⚠️ Deliberately a constant, and deliberately the same method name
        ``ImapFetcher`` uses — ``EmailHarness._clear_fetch_outage`` reads
        ``fetcher._verdict_key`` to clear the standing verdict, and this class
        had none, so a recovered managed inbox was cleared under ``""`` while
        the outage had been recorded under the inbox ADDRESS. The WARN then
        stood on every status seat forever.

        A constant (rather than the address) is what makes the two agree in
        every ordering: a provisioning failure happens BEFORE the address is
        known, so an address-derived key would change between the row that was
        written and the row that must be deleted. There is exactly one managed
        inbox per instance, so one key is enough to name it.
        """
        return "agentmail"

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(word in text for word in
                   ("401", "403", "unauthorized", "forbidden", "api key", "invalid key"))

    def _is_self(self, sender: str) -> bool:
        """Is *sender* the agent's own address? The ONE predicate (D65)."""
        from email.utils import parseaddr

        from core.surfaces.seed import is_self_address
        addr = (parseaddr(sender or "")[1] or "").strip()
        if not addr:
            return False
        if is_self_address(addr):
            return True
        own = (getattr(self.client, "address", None) or "").strip().lower()
        return bool(own) and addr.lower() == own

    def mark_handled(self, handle: Any) -> None:
        # Belt-and-suspenders parity with IMAP's finally-marked ``\Seen``: record
        # the id even when routing errored, so a poison message can't reprocess
        # every poll. Idempotent (INSERT OR IGNORE under route-time ``seen``).
        try:
            self.dedup.seen(str(handle))
        except Exception as e:
            logger.debug("agentmail mark-handled %s failed: %s", handle, e)
