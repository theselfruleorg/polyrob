"""MailFetcher seam — transport-specific "fetch new mail" behind one protocol.

Extracted from EmailHarness (Task 5, 2026-08-18 agent-mail plan) so the poll
loop's dedup/route/act body is transport-blind:

- ``ImapFetcher`` — IMAP on its own connection: UID SEARCH UNSEEN on INBOX,
  ``(UIDVALIDITY, UID)`` handles, ``\\Seen`` as the durable handled-mark.
- ``AgentMailFetcher`` — the managed HTTP inbox. No provider read-state is
  needed: the ``MessageDedup`` store IS the durable handled-mark (read-only
  ``was_seen`` probe here; the recording ``seen`` check stays at route time in
  ``process_email``), so ``mark_handled`` is a no-op.

Both yield ``(handle, normalized_dict)`` pairs where the dict is exactly the
``normalize_email_message`` shape ``process_email`` consumes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Protocol, Tuple

from surfaces.email.dedup import MessageDedup
# The AgentMail wire shape is owned by its CLIENT (tools tier). Imported, never
# re-derived — and imported DOWNWARD, which is what the layering ratchet allows
# (`surfaces` may read `tools`; `tools` may not read `surfaces`).
from tools.email_providers.agentmail import normalize_agentmail_attachments
from tools.email_providers.mime import sender_address, sender_authenticated

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


@dataclass(frozen=True)
class ImapHandle:
    """A message's IDENTITY in the inbox: ``(UIDVALIDITY, UID)``.

    ⚠️ TS-20: the handle used to be a SEQUENCE number, resolved later against
    the tool's SHARED connection. A sequence number is a position: an expunge
    between fetch and mark (the legacy delete verb) shifted it onto another
    message, and a verb that SELECTed another folder made the mark land in that
    folder. The real message then went ``\\Seen`` without a route and the UNSEEN
    poll never saw it again. A UID is stable while other mail comes and goes;
    UIDVALIDITY tells us when even the UID is no longer the same message.
    """
    uidvalidity: str
    uid: bytes

    def key(self) -> str:
        uid = self.uid.decode() if isinstance(self.uid, bytes) else str(self.uid)
        return f"uid:{self.uidvalidity}:{uid}"


@dataclass
class FetchFailure:
    """A message whose FETCH or parse raised (counted by the harness)."""
    handle: ImapHandle
    error: str


class ImapFetcher:
    """The IMAP inbound path on its OWN connection, keyed by UID.

    ⚠️ Every ``imaplib`` call runs in a worker thread, one at a time (EM1): a
    call inside ``async def`` held the gateway's ONE event loop for as long as
    the server took. The connection is the fetcher's own (TS-20) — never the
    tool's ``imap_connection``, which the agent's legacy verbs re-SELECT and
    EXPUNGE while a turn runs.
    """

    def __init__(self, email_tool: Any, *, open_conn: Optional[Callable[[], Any]] = None) -> None:
        self.email_tool = email_tool
        self._open_conn = open_conn
        self._conn: Any = None
        self._lock_pair: Optional[tuple] = None
        #: messages whose fetch/parse raised in the LAST ``fetch_unread``.
        self.last_fetch_failures: List[FetchFailure] = []

    # -- connection -------------------------------------------------------

    def _opener(self) -> Callable[[], Any]:
        if self._open_conn is not None:
            return self._open_conn
        opener = getattr(self.email_tool, "_mbox_open", None)
        if not callable(opener):
            raise MailFetchError("IMAP fetcher: the email tool cannot open a connection")
        return opener

    async def _run(self, fn):
        """``fn(conn)`` off the loop on the fetcher's connection, serialised."""
        loop = asyncio.get_running_loop()
        if self._lock_pair is None or self._lock_pair[0] is not loop:
            self._lock_pair = (loop, asyncio.Lock())
        async with self._lock_pair[1]:
            if self._conn is None:
                self._conn = await asyncio.to_thread(self._opener())
            return await asyncio.to_thread(fn, self._conn)

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await asyncio.to_thread(conn.logout)
            except Exception:
                pass

    async def fetch_unread(self) -> List[Tuple[Any, dict]]:
        import email as _email

        from surfaces.email.harness import normalize_email_message
        tool = self.email_tool
        self.last_fetch_failures = []
        # 057 WS-F: IMAP only. ``ensure_initialized()`` ran the SMTP probe, so a
        # rejected send-side login stopped INBOUND mail and logged an ERROR every
        # 60 s poll (1,439 lines/day on prod). Polling now needs the receive half.
        ensure = getattr(tool, "ensure_imap", None) or getattr(tool, "ensure_initialized", None)
        try:
            if ensure is not None:
                await ensure()
            validity, uids = await self._select_unread()
        except MailFetchError:
            raise
        except Exception as e:
            # D28: an outage is NOT "no new mail". Auth failures carry the flag
            # so the harness can record a durable verdict against the mailbox.
            raise MailFetchError(
                f"IMAP read failed: {type(e).__name__}: {e}",
                auth=self._is_auth_error(e), key=self._verdict_key()) from e
        out: List[Tuple[Any, dict]] = []
        for uid in uids:
            handle = ImapHandle(validity, uid)
            try:
                # D4: PEEK. ``(RFC822)`` sets ``\Seen`` as a side effect of the
                # FETCH itself; ``mark_handled`` remains the ONE place a message
                # is marked, and it runs only once the harness is done with it.
                _, data = await self._run(
                    lambda c, u=uid: c.uid("FETCH", u, "(BODY.PEEK[])"))
                raw = next(p[1] for p in data if isinstance(p, tuple) and len(p) > 1)
                # CHAT-7: MIME + HTML parsing of mail anyone can send runs OFF
                # the event loop (it can be large; it must never stall the bot).
                msg = await asyncio.to_thread(
                    lambda b=raw: normalize_email_message(_email.message_from_bytes(b)))
                out.append((handle, msg))
            except Exception as e:
                logger.warning("email fetch uid %s failed: %s", uid, e, exc_info=True)
                self.last_fetch_failures.append(
                    FetchFailure(handle, f"{type(e).__name__}: {e}"))
        return out

    async def _select_unread(self) -> Tuple[str, List[bytes]]:
        """``(UIDVALIDITY, unseen UIDs)`` for INBOX, reconnecting ONCE if the
        connection turns out to be dead.

        ⚠️ Gmail closes an idle IMAP socket but the ``IMAP4_SSL`` OBJECT
        survives, so a poll that reused it raised ``socket error: EOF`` once a
        minute forever (prod 2026-09-23). Exactly ONE retry: an auth failure is
        not a stale socket, and retrying only hammers the mailbox.
        """
        for attempt in (0, 1):
            try:
                return await self._run(_select_search)
            except Exception as exc:
                if attempt or self._is_auth_error(exc):
                    raise
                logger.info("email inbound: stale IMAP handle (%s) — reconnecting",
                            type(exc).__name__)
                await self._drop()
        raise MailFetchError("IMAP connection unavailable after connect")

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
        """Synchronous form (protocol): marks on the fetcher's own connection
        when it is open. The harness awaits :meth:`mark_handled_async`."""
        try:
            if self._conn is not None and isinstance(handle, ImapHandle):
                _mark_seen(self._conn, handle)
        except Exception as e:
            logger.debug("email mark-seen %s failed: %s", handle, e)

    async def mark_handled_async(self, handle: Any) -> bool:
        """``\\Seen`` on the message's UID in INBOX. False (and logged) when the
        mark could not be made or UIDVALIDITY changed — the message then stays
        UNSEEN and the next poll's row state decides what happens to it."""
        if not isinstance(handle, ImapHandle):
            return False
        for attempt in (0, 1):
            try:
                return await self._run(lambda c: _mark_seen(c, handle))
            except Exception as e:
                if attempt:
                    logger.warning("email mark-seen %s failed: %s", handle.key(), e)
                    return False
                await self._drop()
        return False


def _select_search(c) -> Tuple[str, List[bytes]]:
    c.select("INBOX")
    validity = _uidvalidity(c)
    typ, data = c.uid("SEARCH", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"UID SEARCH answered {typ}")
    raw = data[0] if data and data[0] else b""
    return validity, (raw.split() if raw else [])


def _uidvalidity(c) -> str:
    """The selected mailbox's UIDVALIDITY ("" when the server did not say)."""
    try:
        _, vals = c.response("UIDVALIDITY")
        val = vals[0] if vals else None
    except Exception:
        val = None
    if isinstance(val, bytes):
        val = val.decode("ascii", "replace")
    return str(val or "")


def _mark_seen(c, handle: ImapHandle) -> bool:
    """SELECT INBOX again right before the STORE (a mark must never land in
    another folder), refuse when UIDVALIDITY changed, then flag by UID."""
    c.select("INBOX")
    now = _uidvalidity(c)
    if handle.uidvalidity and now and now != handle.uidvalidity:
        logger.warning("email mark-seen skipped: INBOX UIDVALIDITY changed %s -> %s (%s)",
                       handle.uidvalidity, now, handle.key())
        return False
    typ, _ = c.uid("STORE", handle.uid, "+FLAGS", "(\\Seen)")
    return typ == "OK"


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
            hdrs = full.get("headers") if isinstance(full.get("headers"), dict) else {}
            auth = next((str(v) for k, v in hdrs.items()
                         if str(k).lower() == "authentication-results"), None)
            out.append((mid, {
                "message_id": str(mid),
                "from": sender,
                # CHAT-6: the managed inbox is the receiving MX; its
                # Authentication-Results (when passed) is the only proof of From.
                "sender_authenticated": sender_authenticated(
                    auth, sender_address(sender)),
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
                # OS5: the auto-reply markers, when the provider passes headers.
                "headers": full.get("headers") if isinstance(full.get("headers"), dict) else {},
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
