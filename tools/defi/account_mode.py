"""``account=`` / ``nft=`` on the money verbs — work FROM an NFT's account (069 v4 A3).

The verb builds its call exactly as for the treasury, then this module turns it into a
``TxIntent.via_account`` call: the treasury (the NFT's OWNER) signs
``account.execute(inner, value, data, 0)`` — or, for an ERC-20 spend, the approve → spend →
reset batch (``via_account_batch``) — and the SAME ``tx_guard.authorize`` judges the inner call
with the ACCOUNT as the holder. The verb books the result with ``account=`` (the book's
``account`` column) and appends a signed entry to the account's journal
(``core.wallet.nft_account.append_journal``).

Nothing here authorizes: :func:`resolve` only refuses early with a reason (pinned collection,
code hash, deployed account, ``ownerOf == treasury``); the guard's own pre-flight re-reads the
owner, the lock, the pins and ``state()``. Gate: ``AGENT_NFT_ENABLED`` (default OFF).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional, Tuple

ACCOUNT_DESC = (
    "Optional — work FROM the token-bound (ERC-6551) account of an NFT this treasury OWNS, "
    "instead of from the treasury: the account's assets move, the treasury signs as the NFT's "
    "owner. A 0x account address. Only accounts of a collection the owner pinned. Needs "
    "AGENT_NFT_ENABLED.")
NFT_DESC = (
    "Optional — the same as `account`, naming the NFT instead: '<collection>#<id>', or just "
    "'<id>' when one collection is pinned on the chain.")


def requested(params) -> bool:
    return bool(getattr(params, "account", None) or getattr(params, "nft", None))


def default_rpc(chain: str):
    from core.wallet.simulation import _default_rpc_for
    return _default_rpc_for(chain)


def resolve(params, chain: str, treasury: str, rpc=None):
    """``(HeldNft, None)`` or ``(None, refusal text)``."""
    from core.env import bool_env
    from core.wallet.nft_account import NftAccountError, resolve as _resolve
    if not bool_env("AGENT_NFT_ENABLED", False):
        from core.remedy import flag_remedy
        return None, (f"agent NFTs are off (AGENT_NFT_ENABLED), so nothing works from an NFT's "
                      f"account — {flag_remedy('AGENT_NFT_ENABLED')}. Nothing was broadcast.")
    try:
        held = _resolve(chain, rpc=rpc or default_rpc(chain), treasury=treasury,
                        account=getattr(params, "account", None), nft=getattr(params, "nft", None))
    except NftAccountError as exc:
        return None, f"refused: {exc}. Nothing was broadcast."
    except Exception as exc:  # noqa: BLE001 — fail closed
        return None, f"refused: the account could not be verified ({exc}). Nothing was broadcast."
    return held, None


def wrap(rail, inner: dict, held, rpc=None) -> Tuple[dict, int]:
    """``(outer tx, state)``: ``account.execute(inner.to, inner.value, inner.data, 0)``,
    signed by the treasury, sending no value itself."""
    from core.wallet import erc6551
    rpc = rpc or default_rpc(held.chain)
    state = erc6551.read_state(rpc, held.account)
    data = erc6551.encode_execute(inner["to"], int(inner.get("value") or 0),
                                  inner.get("data") or "0x", 0)
    return rail.build_call(to=held.account, data=data, value=0), state


def wrap_batch(rail, *, token: str, spender: str, grant_raw: int, spend: dict, held,
               rpc=None) -> Tuple[dict, int]:
    """``(outer tx, state)``: ``account.executeBatch([token.approve(spender, grant),
    spend, token.approve(spender, 0)])`` — the W8 approve-spend-reset batch."""
    from core.wallet import abi, erc6551
    rpc = rpc or default_rpc(held.chain)
    state = erc6551.read_state(rpc, held.account)
    approve = lambda n: abi.encode_call(  # noqa: E731
        "approve", [{"type": "address"}, {"type": "uint256"}], [spender, int(n)])
    legs = [(token, 0, approve(grant_raw), 0),
            (spend["to"], int(spend.get("value") or 0), spend.get("data") or "0x", 0),
            (token, 0, approve(0), 0)]
    return rail.build_call(to=held.account, data=erc6551.encode_execute_batch(legs), value=0), state


def intent_for(intent, held, state: int, *, batch: bool = False):
    return dataclasses.replace(intent, via_account=held.account, via_account_state=int(state),
                               via_account_batch=bool(batch))


def header_line(held) -> str:
    return (f"  from account: {held.account} ({held.label}; the treasury signs as the NFT's "
            f"owner)\n")


def journal_line(held, signer, *, kind: str, text: str, refs=()) -> str:
    """Append one signed journal entry; the line to add to the verb's result. Never raises:
    a transaction that landed stands whatever its journal does, and the line says so.

    Every core writer of a journal line goes through here (trade_tool, launchpad, agent_nft
    guarded + withdraw), so the optional site publish hangs off THIS success path, once per
    entry: after the transaction landed and the entry is on disk, never on a failed append."""
    try:
        from core.wallet.nft_account import append_journal, digest
        entry, _path = append_journal(held, signer, kind=kind, text=text, refs=tuple(refs))
    except Exception as exc:  # noqa: BLE001
        return f"\n  journal: NOT written ({exc}) — the transaction above stands"
    out = f"\n  journal: entry #{entry['seq']} ({kind}) signed, head {digest(entry)[:16]}…"
    try:
        from core.wallet.nft_account import publish_after_append
        published = publish_after_append(held.chain_id, held.account)
    except Exception:  # noqa: BLE001 — best effort; never changes the verb's result
        published = ""
    return out + (f"\n  {published}" if published else "")


def swap_kind(intent: Any) -> str:
    """``exit`` for an exit-shaped swap (sells into the chain's quote asset), else ``entry``."""
    try:
        from core.wallet.tx_guard import _intent_is_exit_shaped
        return "exit" if _intent_is_exit_shaped(intent) else "entry"
    except Exception:  # noqa: BLE001
        return "entry"

