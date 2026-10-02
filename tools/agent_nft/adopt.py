"""The core-owned body of ``agent_nft_adopt`` (C3) — start working from an NFT the treasury owns.

ALWAYS owner-approved: the tool refuses it outside a genuine owner turn. The flow:

1. select the NFT (``nft_holdings.select``: pinned collection, code hash, deployed account,
   ``ownerOf == treasury``);
2. read its journal HISTORY from the chain (``JournalLog`` ``Entry`` events, verified — a sold
   account keeps its seller's entries; the local file without a ``journal_log``). An unreadable
   history refuses: adopting over a history this instance cannot see would start a second chain;
3. dry run (default): report what was found and stop;
4. live: hand off to the agent-NFT package's ``adopt`` verb (the owner-side checks — lock, open
   approvals and coverage, identity count — and the pfp, the inherited positions and the
   Account Brief). It reads the same history through ``tool.journal_history(params)`` and writes
   NO journal entry itself;
5. then the ``handover`` entry, ON CHAIN through the account when a ``journal_log`` is pinned
   (``tools/agent_nft/journal_write.py``).
"""
from __future__ import annotations

from typing import Any


async def adopt(tool, params, execution_context) -> Any:
    from core.wallet.nft_account import NftAccountError, digest
    from tools.agent_nft import journal_write
    from tools.agent_nft.withdraw import _held
    try:
        held, signer = _held(tool, params, execution_context)
    except NftAccountError as exc:
        return tool._ar(error=f"{exc}. Nothing was written.")
    try:
        entries = journal_write.history(tool, held)
    except Exception as exc:  # noqa: BLE001 — an unreadable history is not an empty one
        return tool._ar(error=(f"the journal history of {held.account} could not be read ({exc}) — "
                               f"adopting now would start a second chain over it. Nothing was "
                               f"written."))
    owners = sorted({str(e.get("owner")) for e in entries})
    head = digest(entries[-1])[:16] + "…" if entries else "none"
    seen = (f"ADOPT {held.label} ({held.collection} #{held.token_id}) on {held.chain}\n"
            f"  account: {held.account}\n"
            f"  journal: {len(entries)} verified entr{'y' if len(entries) == 1 else 'ies'}"
            f" (head {head}; by {', '.join(owners) or 'nobody yet'})"
            f" — {'on chain, JournalLog ' + held.journal_log if held.journal_log else 'local file'}\n")
    if params.dry_run:
        return tool._ar(content=seen + ("  RESULT: DRY RUN — nothing written. Re-run with "
                                        "dry_run=false to adopt (pfp, inherited positions, Brief, "
                                        "then a handover entry)."))
    fn = getattr(tool._impl(), "adopt", None)
    if fn is None:
        return tool._ar(error=seen + "the installed agent-NFT package has no `adopt` — upgrade it. "
                                     "Nothing was written.")
    res = await fn(tool, params, execution_context)
    if getattr(res, "error", None):
        return tool._ar(error=seen + str(res.error))
    handover = await journal_write.write_entry(
        tool, held=held, signer=signer, kind="handover",
        text=f"adopted {held.label} by {signer.address}; history {len(entries)} entries, head {head}",
        dry_run=False, max_spend_usd=params.max_spend_usd, execution_context=execution_context,
        verb="adopt")
    out = getattr(res, "extracted_content", None) or ""
    tail = getattr(handover, "extracted_content", None) or f"handover: {getattr(handover, 'error', '')}"
    return tool._ar(content=seen + out + "\n" + tail)
