"""One journal entry with NO action, and the account's journal history — core-owned (J1, C3).

* :func:`write_entry` — with a pinned ``journal_log`` the entry is signed, then written ON CHAIN
  as the account's single call ``JournalLog.log(entry)`` (``TxIntent.is_journal_entry``: the
  guard checks the entry and that nothing of the account moves), and kept in the local cache once
  it landed. Without one, it is appended to the local journal. ``agent_nft_adopt``'s handover
  entry and the agent-NFT package's ``journal`` verb (thesis / note) go through here.
* :func:`history` — the account's verified chain: its ``Entry`` events (a sold account keeps its
  seller's entries), or the local file without a ``journal_log``. An unreadable history RAISES.
"""
from __future__ import annotations

from typing import Any, Dict, List


def history(tool, held) -> List[Dict[str, Any]]:
    from core.wallet.nft_account import prior_entries
    return prior_entries(held, rpc=tool.rpc_for(held.chain))


async def write_entry(tool, *, held, signer, kind: str, text: str, dry_run: bool,
                      max_spend_usd: float, execution_context, verb: str = "journal") -> Any:
    from core.wallet import journal_log
    from core.wallet.nft_account import cache_entry, canonical, digest
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    from tools.defi.account_mode import journal_line, prepare_journal
    if not held.journal_log:
        if dry_run:
            return tool._ar(content=(f"JOURNAL {held.label} ({kind}): no JournalLog is pinned for this "
                                     f"collection, so the entry goes to the local journal only. "
                                     f"DRY RUN — nothing written; re-run with dry_run=false."))
        line = journal_line(held, signer, kind=kind, text=text)
        return tool._ar(content=f"JOURNAL {held.label} ({kind}), local journal:{line}")
    rpc = tool.rpc_for(held.chain)
    entry, why = prepare_journal(held, signer, kind=kind, text=text, rpc=rpc)
    if entry is None:
        return tool._ar(error=f"journal: NOT written ({why}). Nothing was broadcast.")
    intent = TxIntent(chain=held.chain, token=None, to=held.journal_log, amount_raw=0,
                      max_spend_usd=max_spend_usd, is_journal_entry=True)
    header = (f"JOURNAL {held.label} ({kind}) entry #{entry['seq']} → JournalLog {held.journal_log}\n"
              f"  through its account {held.account}; head {digest(entry)[:16]}…\n")
    tool._last_receipt = None
    res = await guarded_call(tool, execution_context=execution_context, verb=verb, intent=intent,
                             inner_to=held.journal_log,
                             inner_data=journal_log.encode_log(canonical(entry)),
                             held=held, dry_run=dry_run, rpc=rpc, header=header,
                             hint=digest(entry))
    receipt = getattr(tool, "_last_receipt", None)
    if not dry_run and receipt and getattr(receipt[1], "status", None) in ("success", "pending"):
        try:
            cache_entry(held, entry, rpc=rpc)
        except Exception as exc:  # noqa: BLE001 — the chain holds it; the cache is a convenience
            content = getattr(res, "extracted_content", None) or ""
            res = tool._ar(content=content + f"\n  (local cache NOT updated: {exc})")
    return res
