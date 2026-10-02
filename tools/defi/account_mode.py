"""``account=`` / ``nft=`` on the money verbs — work FROM an NFT's account (069 v4 A3).

The verb builds its call exactly as for the treasury, then this module turns it into a
``TxIntent.via_account`` call: the treasury (the NFT's OWNER) signs
``account.execute(inner, value, data, 0)`` — or, for an ERC-20 spend, the approve → spend →
reset batch (``via_account_batch``) — and the SAME ``tx_guard.authorize`` judges the inner call
with the ACCOUNT as the holder. The verb books the result with ``account=`` (the book's
``account`` column) and writes a signed entry to the account's journal: with a pinned
``journal_log`` the entry is signed BEFORE the broadcast and rides the same ``executeBatch`` as
its last leg, ``JournalLog.log(entry)`` (J1; the local file is a cache); without one it is
appended to the local journal after the receipt (``core.wallet.nft_account``).

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


def prepare_journal(held, signer, *, kind: str, text: str, rpc=None):
    """J1: ``(entry, "")`` — the signed entry this action carries ON CHAIN as the last leg of its
    ``executeBatch`` — or ``(None, why)``: no ``journal_log`` pinned (``why`` empty: the local
    journal is written after the receipt, as before), or the entry could not be built (``why``
    names it; the action goes out without a journal leg and its result says so). Never raises."""
    if not getattr(held, "journal_log", None):
        return None, ""
    try:
        from core.wallet.nft_account import prepare_entry
        return prepare_entry(held, signer, kind=kind, text=text,
                             rpc=rpc or default_rpc(held.chain)), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _journal_leg(held, entry) -> Tuple[str, int, str, int]:
    from core.wallet import journal_log
    from core.wallet.nft_account import canonical
    return (held.journal_log, 0, journal_log.encode_log(canonical(entry)), 0)


def wrap(rail, inner: dict, held, rpc=None, *, journal=None) -> Tuple[dict, int]:
    """``(outer tx, state)``: ``account.execute(inner.to, inner.value, inner.data, 0)``,
    signed by the treasury, sending no value itself. With *journal* (a signed entry from
    :func:`prepare_journal`): ``account.executeBatch([inner, JournalLog.log(entry)])``."""
    from core.wallet import erc6551
    rpc = rpc or default_rpc(held.chain)
    state = erc6551.read_state(rpc, held.account)
    leg = (inner["to"], int(inner.get("value") or 0), inner.get("data") or "0x", 0)
    if journal is not None:
        data = erc6551.encode_execute_batch([leg, _journal_leg(held, journal)])
    else:
        data = erc6551.encode_execute(*leg)
    return rail.build_call(to=held.account, data=data, value=0), state


def wrap_batch(rail, *, token: str, spender: str, grant_raw: int, spend: dict, held,
               rpc=None, journal=None) -> Tuple[dict, int]:
    """``(outer tx, state)``: ``account.executeBatch([token.approve(spender, grant),
    spend, token.approve(spender, 0)])`` — the W8 approve-spend-reset batch; with *journal*,
    ``JournalLog.log(entry)`` as a fourth leg."""
    from core.wallet import abi, erc6551
    rpc = rpc or default_rpc(held.chain)
    state = erc6551.read_state(rpc, held.account)
    approve = lambda n: abi.encode_call(  # noqa: E731
        "approve", [{"type": "address"}, {"type": "uint256"}], [spender, int(n)])
    legs = [(token, 0, approve(grant_raw), 0),
            (spend["to"], int(spend.get("value") or 0), spend.get("data") or "0x", 0),
            (token, 0, approve(0), 0)]
    if journal is not None:
        legs.append(_journal_leg(held, journal))
    return rail.build_call(to=held.account, data=erc6551.encode_execute_batch(legs), value=0), state


def intent_for(intent, held, state: int, *, batch: bool = False, journal: bool = False):
    return dataclasses.replace(intent, via_account=held.account, via_account_state=int(state),
                               via_account_batch=bool(batch), via_account_journal=bool(journal))


def header_line(held) -> str:
    return (f"  from account: {held.account} ({held.label}; the treasury signs as the NFT's "
            f"owner)\n")


def journal_line(held, signer, *, kind: str, text: str, refs=(), entry=None, landed: bool = True,
                 skipped: str = "", rpc=None) -> str:
    """The journal line to add to the verb's result. Never raises: a transaction that landed
    stands whatever its journal does, and the line says so.

    * *entry* (J1) — the signed entry rode the transaction as its ``JournalLog.log`` leg: when
      the transaction *landed* (confirmed or in flight) it is kept in the local cache; when it
      reverted, the entry reverted with it.
    * *skipped* — an on-chain journal is pinned but the entry could not be built before the
      broadcast (the reason); nothing is written, so the chain stays the one on chain.
    * neither — no ``journal_log`` pinned (or an act with no account batch): the entry is signed
      and appended to the local journal now, after the receipt."""
    if entry is not None:
        if not landed:
            return "\n  journal: no entry — the transaction reverted, and its journal leg with it"
        try:
            from core.wallet.nft_account import cache_entry, digest
            cache_entry(held, entry, rpc=rpc)
            cached = ""
        except Exception as exc:  # noqa: BLE001
            from core.wallet.nft_account import digest
            cached = f" (local cache NOT updated: {exc})"
        return (f"\n  journal: entry #{entry['seq']} ({entry['kind']}) signed and logged on chain "
                f"(JournalLog {held.journal_log}) in this transaction, head {digest(entry)[:16]}…"
                + cached)
    if skipped:
        return (f"\n  journal: NOT written ({skipped}) — the entry could not be built before the "
                f"broadcast; the transaction above stands")
    try:
        from core.wallet.nft_account import append_journal, digest
        entry, _path = append_journal(held, signer, kind=kind, text=text, refs=tuple(refs))
    except Exception as exc:  # noqa: BLE001
        return f"\n  journal: NOT written ({exc}) — the transaction above stands"
    return f"\n  journal: entry #{entry['seq']} ({kind}) signed, head {digest(entry)[:16]}…"


def swap_kind(intent: Any) -> str:
    """``exit`` for an exit-shaped swap (sells into the chain's quote asset), else ``entry``."""
    try:
        from core.wallet.tx_guard import _intent_is_exit_shaped
        return "exit" if _intent_is_exit_shaped(intent) else "entry"
    except Exception:  # noqa: BLE001
        return "entry"

