"""The core-owned bodies of ``agent_nft_snapshot`` / ``_inspect`` / ``_journal`` / ``_bind_identity``
(C20 — moved from the agent-NFT package, which keeps only a collection's own parts).

They need no package. A collection's own lines (its face, its reveal state) come from the
package's optional hook ``verbs.extend_view(view, rpc) -> list[str]``; a hook that fails is
named on the result, never fatal.

* snapshot — the account view of the NFT this treasury works from, plus its journal (count,
  head, where: on chain with a pinned ``journal_log``, else the local file);
* inspect — the same view for ANY NFT of a pinned collection (read-only, before buying);
* both add the pinned sale's ``phase()``, ``settled()`` and ``owed()`` (``tools/agent_nft/sale.py``);
* journal — a thesis or note: on chain through the account (``journal_write.write_entry``), and
  with ``anchor`` the head goes to the account's ERC-8004 identity as metadata
  ``<prefix>.journal`` (``polyrob.journal`` for the POLYROB collection), through the account;
* bind — the agent's card on the account's ERC-8004 identity, THROUGH the account: ``register()``
  when it has none, else ``setAgentURI(agentId, card)`` on the identity it owns (115 W6; never a
  second ``register``: it is not idempotent).
"""
from __future__ import annotations

from typing import Any, List

#: What ``agent_nft_journal`` writes; ``entry``/``exit``/``tend`` are the money verbs',
#: ``handover`` is a kind older entries carry (nothing writes it now; it stays readable).
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


def _sale(v, rpc, profile, treasury=None) -> List[str]:
    """The pinned sale's phase, settled and owed lines (115 W8), read at the view's block."""
    from tools.agent_nft import view as V
    from tools.agent_nft.sale import sale_lines
    try:
        return sale_lines(V.pinned_rpc(rpc, v.block), profile, treasury=treasury, account=v.account)
    except Exception as exc:  # noqa: BLE001 — the sale lines are never fatal
        return [f"  sale:      UNREADABLE ({exc})"]


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
    return tool._ar(content=V.render(v, extra=[journal, *_sale(v, rpc, profile, signer.address),
                                               *_extend(tool, v, rpc)]))


async def inspect(tool, params, execution_context) -> Any:
    from tools.agent_nft import view as V
    try:
        chain, profile, token_id = V.resolve_target(params.target)
        rpc = tool.rpc_for(chain)
        v = V.build_view(rpc, chain, profile, token_id)
    except V.ViewError as exc:
        return tool._ar(error=str(exc))
    return tool._ar(content=V.render(v, extra=[*_sale(v, rpc, profile), *_extend(tool, v, rpc)]))


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
                               f"are written by the money verbs"))
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


def _account_agent_id(rpc, held):
    """The identity the account owns: the collection's ``agentIdOf`` for a kind whose collection
    names identities (ERC-6551P: registered at mint), else the registry's log scan for an
    identity minted to the account (``bind``'s own ``register``). None = no identity. Raises
    when unreadable (never "none")."""
    from core.wallet import collection_registry, erc8004_reputation
    from tools.defi.agent_registration import read_agent_id
    profile = collection_registry.profile_for(held.chain_id, held.collection)
    version = profile.version_for_account(int(held.token_id), held.account)
    if version is not None and version.kind.collection_iface:
        own = erc8004_reputation.agent_id_of(rpc, held.collection, int(held.token_id))
        if own is not None:
            return int(own)
    return read_agent_id(rpc, chain=held.chain, holder=held.account)


async def bind(tool, params, execution_context) -> Any:
    """Make the account's ERC-8004 identity carry the agent's card (115 W6, G3): ``register``
    through the account when it has no identity, else ``setAgentURI(agentId, card)`` through the
    account — the card published on the identity the account already owns (an ERC-6551P token's
    identity from its mint). One verb: the same file, the same guard, the same owner approval."""
    import asyncio
    from core.wallet.nft_account import NftAccountError
    from tools.agent_nft import view as V
    from tools.agent_nft.guarded import guarded_call
    from tools.agent_nft.withdraw import _held
    from tools.defi.agent_registration import account_agent_uri, encode_register
    try:
        held, _signer = _held(tool, params, execution_context)
        rpc = tool.rpc_for(held.chain)
        existing = await asyncio.to_thread(_account_agent_id, rpc, held)
    except (NftAccountError, V.ViewError) as exc:
        return tool._ar(error=f"{exc}. Nothing was broadcast.")
    except Exception as exc:  # noqa: BLE001 — an unread identity count is not zero
        return tool._ar(error=f"the account's identity count could not be read ({exc}). Nothing was "
                              f"broadcast.")
    if existing is not None:
        return await _publish_card(tool, params, execution_context, held, rpc, existing)
    # The file names the ACCOUNT as the agent's wallet (the registry records the same address).
    try:
        agent_uri = account_agent_uri(held.chain_id, held.account)
    except Exception:
        return tool._ar(error="The account's registration document could not be safely built; "
                              "nothing was broadcast")
    if params.agent_uri and params.agent_uri != agent_uri:
        return tool._ar(error="Bind uses the account's own registration document; an alternate "
                              "agent_uri is refused")
    registry, intent = _registration_intent(held.chain, expects_mint=True,
                                            max_spend_usd=params.max_spend_usd)
    return await guarded_call(tool, execution_context=execution_context, verb="bind", intent=intent,
                              inner_to=registry, inner_data=encode_register(agent_uri),
                              held=held, dry_run=params.dry_run, rpc=rpc,
                              header=f"BIND — register() THROUGH the account {held.account}\n")


#: The quote line of a card published through an ERC-6551P account: it is an account call.
CARD_BATCH_NOTE = ("  account:  this is a call THROUGH the account: it ends the NFT's sale readiness "
                   "if it is ready (agent_nft_sale_ready makes it ready again)\n")


async def _publish_card(tool, params, execution_context, held, rpc, agent_id: int) -> Any:
    """``setAgentURI(agentId, card)`` through the account; no transaction when the registry
    already holds this card."""
    import asyncio
    from core.wallet import identity_card
    from tools.agent_nft.guarded import guarded_call
    from tools.defi.agent_registration import account_agent_uri, encode_set_agent_uri
    registry, intent = _registration_intent(held.chain, expects_mint=False,
                                            max_spend_usd=params.max_spend_usd)
    try:
        agent_uri = account_agent_uri(held.chain_id, held.account, agent_id=agent_id,
                                      registry=registry)
    except Exception:
        return tool._ar(error="The account's registration document could not be safely built; "
                              "nothing was broadcast")
    if params.agent_uri and params.agent_uri != agent_uri:
        return tool._ar(error="Bind uses the account's own registration document; an alternate "
                              "agent_uri is refused")
    try:
        current = await asyncio.to_thread(identity_card.read_token_uri, rpc, held.chain, agent_id)
    except Exception as exc:  # noqa: BLE001 — unread is not "different": say it and go on
        current, unread = None, f"  card now: UNREADABLE ({exc})\n"
    else:
        unread = ""
        if current == agent_uri:
            return tool._ar(content=(f"PUBLISH CARD — agentId {agent_id} on {held.chain} already "
                                     f"publishes this card: no transaction"))
    header = (f"PUBLISH CARD — setAgentURI(agentId {agent_id}) THROUGH the account "
              f"{held.account}\n  registry: {registry} ({held.chain})\n" + unread
              + ("  card now: a different file\n" if current else "") + CARD_BATCH_NOTE)
    return await guarded_call(tool, execution_context=execution_context, verb="bind", intent=intent,
                              inner_to=registry, inner_data=encode_set_agent_uri(agent_id, agent_uri),
                              held=held, dry_run=params.dry_run, rpc=rpc, header=header)
