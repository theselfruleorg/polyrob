"""The core-owned bodies of ``agent_nft_withdraw_token`` and ``agent_nft_revoke_all`` (069 v4 A4).

Core owns them because the guard's 069 §5 rule 5 names ``agent_nft_revoke_all`` as the remedy
for an NFT that may not leave with open approvals — a remedy must not depend on the optional
agent-NFT package — and because ``/nft send`` (the owner's seat) runs the withdraw.

* withdraw: ``safeTransferFrom(treasury, to, id)`` on the pinned collection, signed by the treasury
  (the NFT's owner), declared as ``nft_out``. The guard judges the move: the nesting rules (no
  token-bound account of a pinned collection, deployed or not yet minted, as ``to``), the code
  pin, and rule 5 (no open approval on the token's account, complete scan). ALWAYS
  owner-approved (lane ``owner_always``).
* revoke_all: every open approval the scan finds on the account, one guarded call THROUGH the
  account each (ERC-20 ``approve(s, 0)``, ``setApprovalForAll(op, false)``, ERC-721
  ``approve(0, id)``, Permit2 ``approve(token, s, 0, 0)``, ERC-6909 ``approve(s, id, 0)`` and
  ``setOperator(op, false)``).
"""
from __future__ import annotations

from typing import Any


def _held(tool, params, execution_context):
    """``(HeldNft, signer)`` or raises NftAccountError (the reason is shown)."""
    from core.wallet import nft_holdings
    from core.wallet.nft_account import NftAccountError
    wallet = tool._get_wallet()
    if wallet is None:
        raise NftAccountError("agent wallet not enabled (AGENT_WALLET_ENABLED)")
    signer = wallet.operational_signer()
    held = nft_holdings.select(params.chain, getattr(params, "nft", None),
                               account=getattr(params, "account", None),
                               rpc=tool.rpc_for(params.chain), treasury=signer.address)
    return held, signer


async def withdraw(tool, params, execution_context) -> Any:
    from core.wallet import abi
    from core.wallet.nft_account import NftAccountError
    from core.wallet.tokens import normalize_address
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    try:
        to = normalize_address(params.to)
        held, signer = _held(tool, params, execution_context)
    except (NftAccountError, ValueError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    if to.lower() == signer.address.lower():
        return tool._ar(error="the destination is this treasury itself — nothing would move. "
                              "Nothing was broadcast.")
    # C15: safeTransferFrom — a contract recipient that cannot hold an ERC-721 (no
    # onERC721Received) reverts in the simulation instead of locking the NFT and its account.
    data = abi.encode_call("safeTransferFrom", [{"type": "address"}, {"type": "address"},
                                                {"type": "uint256"}],
                           [signer.address, to, held.token_id])
    intent = TxIntent(chain=params.chain, token=None, to=held.collection, amount_raw=0,
                      max_spend_usd=params.max_spend_usd, is_nft_op=True,
                      nft_out=((held.collection, "erc721", held.token_id, 1),))
    header = (f"SEND {held.label} ({held.collection} #{held.token_id}) → {to}\n"
              f"  its account {held.account} goes with it; approvals on it must be cleared first\n")
    tool._last_receipt = None
    res = await guarded_call(tool, execution_context=execution_context, verb="withdraw_token",
                             intent=intent, inner_to=held.collection, inner_data=data,
                             dry_run=params.dry_run, header=header,
                             hint=signer.address.lower(),
                             asset=f"erc721:{held.collection}:{held.token_id}", counterparty=to)
    receipt = getattr(tool, "_last_receipt", None)
    if not params.dry_run and receipt and getattr(receipt[1], "status", None) == "success":
        from tools.defi.account_mode import journal_line
        extra = journal_line(held, signer, kind="handover",
                             text=f"sent {held.label} to {to}: tx {receipt[0]}", refs=(receipt[0],))
        content = getattr(res, "extracted_content", None)
        if content:
            res = tool._ar(content=content + extra)
    return res


async def revoke_all(tool, params, execution_context) -> Any:
    from core.wallet import abi, erc6551
    from core.wallet import collection_registry
    from core.wallet.nft_account import NftAccountError
    from core.wallet.tx_guard import TxIntent
    from tools.agent_nft.guarded import guarded_call
    try:
        held, _signer = _held(tool, params, execution_context)
        profile = collection_registry.profile_for(held.chain_id, held.collection)
        rpc = tool.rpc_for(params.chain)
        head = int(rpc("eth_blockNumber", []), 16)
        rows = erc6551.open_approvals(rpc, held.account, profile.deploy_block, to_block=head)
    except NftAccountError as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    except Exception as exc:  # noqa: BLE001 — an unreadable table is not an empty one
        return tool._ar(error=f"the approval scan did not complete ({exc}) — the table is "
                              f"INCOMPLETE, not empty. Nothing was broadcast.")
    if not rows:
        return tool._ar(content=(f"no open approvals on {held.account} ({held.label}) — scanned "
                                 f"blocks {profile.deploy_block}..{head}, kinds "
                                 f"{', '.join(erc6551.APPROVAL_KINDS)} (complete). Nothing to revoke."))
    per = float(params.max_spend_usd) / len(rows)
    lines = []
    for a in rows:
        inner_to = a.contract
        if a.kind == "erc20":
            data = abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}], [a.spender, 0])
            intent = TxIntent(chain=params.chain, token=a.contract, to=a.spender, amount_raw=0,
                              max_spend_usd=per, is_allowance_op=True)
            what = f"erc20 {a.contract} → {a.spender}"
        elif a.kind == "operator":
            data = abi.encode_call("setApprovalForAll", [{"type": "address"}, {"type": "bool"}],
                                   [a.spender, False])
            intent = TxIntent(chain=params.chain, token=None, to=a.contract, amount_raw=0,
                              max_spend_usd=per, is_nft_op=True,
                              nft_operator_ops=((a.contract, a.spender, False),))
            what = f"operator {a.contract} → {a.spender}"
        elif a.kind == "erc721":
            data = erc6551.encode_erc721_revoke(int(a.token_id))
            intent = TxIntent(chain=params.chain, token=None, to=a.contract, amount_raw=0,
                              max_spend_usd=per, is_nft_op=True,
                              nft_approval_revokes=((a.contract, int(a.token_id)),))
            what = f"erc721 {a.contract} #{a.token_id} → {a.spender}"
        elif a.kind == "erc6909":
            data = erc6551.encode_erc6909_revoke(a.spender, int(a.token_id))
            intent = TxIntent(chain=params.chain, token=None, to=a.contract, amount_raw=0,
                              max_spend_usd=per, is_nft_op=True,
                              erc6909_revokes=((a.contract, a.spender, int(a.token_id)),))
            what = f"erc6909 {a.contract} #{a.token_id} → {a.spender}"
        elif a.kind == "erc6909_operator":
            data = erc6551.encode_erc6909_operator_revoke(a.spender)
            intent = TxIntent(chain=params.chain, token=None, to=a.contract, amount_raw=0,
                              max_spend_usd=per, is_nft_op=True,
                              nft_operator_ops=((a.contract, a.spender, False),))
            what = f"erc6909 operator {a.contract} → {a.spender}"
        elif a.kind == "permit2":
            inner_to = erc6551.PERMIT2
            data = erc6551.encode_permit2_revoke(a.contract, a.spender)
            intent = TxIntent(chain=params.chain, token=a.contract, to=erc6551.PERMIT2, amount_raw=0,
                              max_spend_usd=per, is_allowance_op=True,
                              permit2_revokes=((a.contract, a.spender),))
            what = f"permit2 {a.contract} → {a.spender}"
        else:
            lines.append(f"NOT revoked (unknown kind {a.kind}): {a.contract} → {a.spender}")
            continue
        res = await guarded_call(tool, execution_context=execution_context, verb="revoke_all",
                                 intent=intent, inner_to=inner_to, inner_data=data, held=held,
                                 dry_run=params.dry_run, rpc=rpc,
                                 header=f"REVOKE {what} through {held.account} (fee cap ${per:.4f})\n",
                                 journal_kind="tend", journal_text=f"revoke {what}")
        lines.append(getattr(res, "extracted_content", None) or getattr(res, "error", None) or str(res))
    return tool._ar(content="\n".join(lines))
