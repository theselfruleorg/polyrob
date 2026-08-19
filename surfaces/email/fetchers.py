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

logger = logging.getLogger(__name__)


class MailFetcher(Protocol):
    async def fetch_unread(self) -> List[Tuple[Any, dict]]:
        """New messages as (handle, normalized dict) pairs."""
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
        await tool.ensure_initialized()
        if not getattr(tool, "imap_connection", None):
            await tool._connect_imap()
        conn = tool.imap_connection
        conn.select("INBOX")
        _, nums = conn.search(None, "UNSEEN")
        out: List[Tuple[Any, dict]] = []
        for num in (nums[0].split() if nums and nums[0] else []):
            try:
                _, data = conn.fetch(num, "(RFC822)")
                em = _email.message_from_bytes(data[0][1])
                out.append((num, normalize_email_message(em)))
            except Exception as e:
                logger.debug("email fetch %s failed: %s", num, e)
        return out

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
        try:
            summaries = await self.client.list_messages(limit=self.limit)
        except Exception as e:
            logger.debug("agentmail list failed: %s", e)
            return []
        own = (getattr(self.client, "address", None) or "").lower()
        out: List[Tuple[Any, dict]] = []
        for summary in summaries:
            mid = summary.get("message_id")
            if not mid or self.dedup.was_seen(str(mid)):
                continue
            try:
                full = await self.client.get_message(mid)
            except Exception as e:
                logger.debug("agentmail get %s failed: %s", mid, e)
                continue
            sender = str(full.get("from") or "")
            if own and own in sender.lower():
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
            }))
        return out

    def mark_handled(self, handle: Any) -> None:
        # Belt-and-suspenders parity with IMAP's finally-marked ``\Seen``: record
        # the id even when routing errored, so a poison message can't reprocess
        # every poll. Idempotent (INSERT OR IGNORE under route-time ``seen``).
        try:
            self.dedup.seen(str(handle))
        except Exception as e:
            logger.debug("agentmail mark-handled %s failed: %s", handle, e)
