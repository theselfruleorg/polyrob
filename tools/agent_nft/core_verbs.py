"""The core-owned bodies of ``agent_nft_snapshot`` / ``_inspect`` / ``_journal`` / ``_bind_identity``
(C20 — moved from the agent-NFT package, which keeps only a collection's own parts).

They need no package. A collection's own lines (its face, its reveal state) come from the
package's optional hook ``verbs.extend_view(view, rpc) -> list[str]``; a hook that fails is
named on the result, never fatal.

* snapshot — the account view of the NFT this treasury works from, plus its journal (count,
  head, where: on chain with a pinned ``journal_log``, else the local file);
* inspect — the same view for ANY NFT of a pinned collection (read-only, before buying);
* journal — a thesis or note: on chain through the account (``journal_write.write_entry``), and
  with ``anchor`` the head goes to the account's ERC-8004 identity as metadata
  ``<prefix>.journal`` (``polyrob.journal`` for the POLYROB collection), through the account;
* bind — ``register()`` on the ERC-8004 identity registry THROUGH the account (refused if the
  account already holds one: ``register()`` is not idempotent).
"""
from __future__ import annotations

from typing import Any, List

#: What ``agent_nft_journal`` writes; ``entry``/``exit``/``tend`` are the money verbs',
#: ``handover`` is adopt's.
AUTHOR_KINDS = ("thesis", "note")


def anchor_key(held) -> str:
    return f"{(held.journal_prefix or 'agent').lower()}.journal"


def _extend(tool, view, rpc) -> List[str]:
    hook = getattr(tool._impl(), "extend_view", None) if tool._impl() is not None else None
    if hook is None:
        return []
    try:
        return [str(x) for x in (hook(view, rpc) or [])]
    except Exception as exc:  # noqa: BLE001 — a collection's own lines are never fatal
        return [f"  (the agent-NFT package's lines failed: {exc})"]


async def snapshot(tool, params, execution_context) -> Any:
    from core.wallet import collection_registry
    from core.wallet.nft_account import NftAccountError, digest
    from tools.agent_nft import journal_write, view as V
    from tools.agent_nft.withdraw import _held
    try:
        held, signer = _held(tool, params, execution_context)
        rpc = tool.rpc_for(held.chain)
        profile = collection_registry.profile_for(held.chain_id, held.collection)
        v = V.build_view(rpc, held.chain, profile, held.token_id, treasury=signer.address)
    except (NftAccountError, V.ViewError) as exc:
        return tool._ar(error=str(exc))
    try:
        entries = journal_write.history(tool, held)
        head = digest(entries[-1])[:16] + "…" if entries else "none"
        where = f"on chain, JournalLog {held.journal_log}" if held.journal_log else "local file"
        journal = f"  journal:   {len(entries)} entries, head {head} ({where})"
    except Exception as exc:  # noqa: BLE001 — an unreadable history is not an empty one
        journal = f"  journal:   UNREADABLE ({exc})"
    return tool._ar(content=V.render(v, extra=[journal, *_extend(tool, v, rpc)]))


async def inspect(tool, params, execution_context) -> Any:
    from tools.agent_nft import view as V
    try:
        chain, profile, token_id = V.resolve_target(params.target)
        rpc = tool.rpc_for(chain)
        v = V.build_view(rpc, chain, profile, token_id)
    except V.ViewError as exc:
        return tool._ar(error=str(exc))
    return tool._ar(content=V.render(v, extra=_extend(tool, v, rpc)))


def _registration_intent(chain: str, *, expects_mint: bool, max_spend_usd: float):
    from core.wallet import erc8004
    from core.wallet.tx_guard import TxIntent
    registry = erc8004.resolve_identity_registry(chain)
    return registry, TxIntent(chain=chain, token=None, to=registry, amount_raw=0,
                              max_spend_usd=float(max_spend_usd), is_registration=True,
                              expected_registry=registry, expects_mint=expects_mint)


async def journal(tool, params, execution_context) -> Any:
    from core.wallet import abi, collection_registry
    from core.wallet.nft_account import NftAccountError, digest
    from tools.agent_nft import journal_write, view as V
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.withdraw import _held
    if params.kind not in AUTHOR_KINDS:
        return tool._ar(error=(f"agent_nft_journal writes {' or '.join(AUTHOR_KINDS)}; entry/exit/tend "
                               f"are written by the money verbs, handover by adopt"))
    try:
        held, signer = _held(tool, params, execution_context)
    except NftAccountError as exc:
        return tool._ar(error=f"{exc}. Nothing was written.")
    res = await journal_write.write_entry(
        tool, held=held, signer=signer, kind=params.kind, text=params.text, dry_run=params.dry_run,
        max_spend_usd=params.max_spend_usd, execution_context=execution_context)
    if getattr(res, "error", None) or params.dry_run or not params.anchor:
        return res
    out = getattr(res, "extracted_content", None) or ""
    try:
        rpc = tool.rpc_for(held.chain)
        entries = journal_write.history(tool, held)
        if not entries:
            raise V.ViewError("the entry is not in the history yet")
        profile = collection_registry.profile_for(held.chain_id, held.collection)
        agent_id = V.identity_agent_id(rpc, held.chain, held.account, profile.deploy_block,
                                       int(rpc("eth_blockNumber", []), 16))
    except Exception as exc:  # noqa: BLE001
        return tool._ar(content=out + f"\n  anchor: skipped — {exc}")
    if agent_id is None:     # agentId 0 is a real id
        return tool._ar(content=out + "\n  anchor: skipped — the account has no identity yet "
                                      "(run agent_nft_bind_identity)")
    registry, intent = _registration_intent(held.chain, expects_mint=False,
                                            max_spend_usd=params.max_spend_usd)
    data = abi.encode_call("setMetadata", [{"type": "uint256"}, {"type": "string"}, {"type": "bytes"}],
                           [int(agent_id), anchor_key(held), bytes.fromhex(digest(entries[-1]))])
    anchored = await guarded_call(tool, execution_context=execution_context, verb="journal",
                                  intent=intent, inner_to=registry, inner_data=data, held=held,
                                  dry_run=False, rpc=rpc,
                                  header=f"ANCHOR {anchor_key(held)} = head through the account\n")
    return tool._ar(content=out + "\n" + (getattr(anchored, "extracted_content", None)
                                          or f"  anchor: {getattr(anchored, 'error', '')}"))


async def bind(tool, params, execution_context) -> Any:
    from core.wallet.nft_account import NftAccountError
    from tools.agent_nft import view as V
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.withdraw import _held
    from tools.defi.agent_registration import encode_register
    try:
        held, _signer = _held(tool, params, execution_context)
        rpc = tool.rpc_for(held.chain)
        if V.identity_balance(rpc, held.chain, held.account) > 0:
            raise V.ViewError("the account already holds an ERC-8004 identity — register() is not "
                              "idempotent and a second call splits it")
    except (NftAccountError, V.ViewError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    except Exception as exc:  # noqa: BLE001 — an unread identity count is not zero
        return tool._ar(error=f"the account's identity count could not be read ({exc}). Nothing was "
                              f"broadcast.")
    registry, intent = _registration_intent(held.chain, expects_mint=True,
                                            max_spend_usd=params.max_spend_usd)
    return await guarded_call(tool, execution_context=execution_context, verb="bind", intent=intent,
                              inner_to=registry, inner_data=encode_register(params.agent_uri),
                              held=held, dry_run=params.dry_run, rpc=rpc,
                              header=f"BIND — register() THROUGH the account {held.account}\n")
