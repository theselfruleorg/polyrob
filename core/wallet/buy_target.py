"""A run's declared money target (068 G2).

The 2026-09-25 buyback carried its target in PROSE: the cron text said "PNL",
three rewrites had dropped the address, and the model filled the gap from the
first matching line it read. A goal or cron payload may now declare the target
as DATA:

    payload["target_token"] = {"chain": "robinhood", "address": "0xbBa6…2c7e"}

It is normalized when the job is created and again when the run starts, rides
the session (``SessionRequest.money_target`` → ``orchestrator._money_target`` →
``ActionExecutionContext.metadata["money_target"]``), and the buy verbs refuse
any acquisition of a non-canonical token other than the target, on any chain
(canonical USDC / wrapped native / native stay free to move).

It RESTRICTS for everyone: a run with no target is unchanged, moving working
capital between canonical assets is unchanged, and an agent-authored target can
never widen what a verb allows — an agent-authored job may declare one too.

W0: an OWNER-authored target is also TRUST for that run. The owner wrote the
address into the job: an owner SEAT (``/cron add … target=``, the CLI, the
console), or an owner-authored chat turn whose owner message names the address
VERBATIM (DEFI-5; otherwise ``target_authored_by: agent`` keeps it
restrict-only). So the identity gate
treats that contract as verified for this run only (``owner_target_matches``).
The runner stamps ``authored_by: owner`` onto the run's target; the model never
writes the run metadata, so it cannot forge the stamp.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

PAYLOAD_KEY = "target_token"
METADATA_KEY = "money_target"
#: Stamped on the RUN's target (never on a stored payload) when the job that
#: declared it is owner-authored.
AUTHOR_KEY = "authored_by"
OWNER = "owner"
AGENT = "agent"
#: Stored on a goal/cron PAYLOAD: ``agent`` when the row is owner-authored but
#: the owner did not type its target address (DEFI-5) — the target restricts
#: and is never identity trust.
TARGET_AUTHOR_KEY = "target_authored_by"


def normalize_target(raw: Any) -> Optional[Dict[str, str]]:
    """``{"chain", "address"}`` in canonical form, None when absent.

    Raises ``ValueError`` on a malformed target — a target that cannot be read
    must stop the run, never silently become "no target".
    """
    if raw is None or raw == {} or raw == "":
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{PAYLOAD_KEY} must be {{'chain': ..., 'address': ...}}, got {raw!r}")
    chain = str(raw.get("chain") or "").strip().lower()
    address = raw.get("address")
    if not chain or not address:
        raise ValueError(f"{PAYLOAD_KEY} needs both 'chain' and 'address'")
    from core.wallet.addresses import normalize_for_chain
    return {"chain": chain, "address": normalize_for_chain(chain, str(address))}


def with_authorship(target: Optional[Dict[str, str]],
                    payload: Any) -> Optional[Dict[str, str]]:
    """The run's target, stamped ``authored_by: owner`` when the job/goal that
    declared it is OWNER-authored (``core.config_policy.rigs.is_owner_authored``,
    a positive stamp — an unstamped legacy row is not trusted)."""
    if not target:
        return target
    from core.config_policy.rigs import is_owner_authored
    out = {"chain": target["chain"], "address": target["address"]}
    p = payload if isinstance(payload, dict) else {}
    if is_owner_authored(p) and p.get(TARGET_AUTHOR_KEY) != AGENT:
        out[AUTHOR_KEY] = OWNER
    return out


def address_in_owner_text(execution_context, address: Optional[str]) -> bool:
    """DEFI-5: True when *address* stands VERBATIM in the owner's own message
    for this turn (``metadata["owner_text"]``, set at the drain). An EVM address
    matches without case (checksum case is cosmetic); any other chain's address
    is case-sensitive (base58)."""
    meta = getattr(execution_context, "metadata", None)
    text = meta.get("owner_text") if isinstance(meta, dict) else None
    addr = str(address or "").strip()
    if not addr or not isinstance(text, str):
        return False
    if addr.lower().startswith("0x"):
        return addr.lower() in text.lower()
    return addr in text


def target_provenance(execution_context, target: Optional[Dict[str, str]],
                      owner_authored: bool) -> Optional[str]:
    """The ``TARGET_AUTHOR_KEY`` stamp for a target an owner-authored row
    declares, or None. A target the owner did not TYPE in this turn is the
    model's choice — restrict-only (``AGENT``) even on an owner-authored row."""
    if not target or not owner_authored:
        return None
    return None if address_in_owner_text(execution_context, target.get("address")) else AGENT


def owner_target_matches(execution_context, *, chain: str,
                         token: Optional[str]) -> bool:
    """True when *token* on *chain* IS this run's OWNER-authored target."""
    target = target_from_context(execution_context)
    if target is None or target.get(AUTHOR_KEY) != OWNER or not token:
        return False
    from core.wallet.addresses import same_address
    return (str(chain or "").strip().lower() == target["chain"]
            and same_address(token, target["address"]))


def target_from_context(execution_context) -> Optional[Dict[str, str]]:
    """The run's target, or None. A malformed stored value reads as None only
    because it was validated at bind time; this never raises."""
    meta = getattr(execution_context, "metadata", None)
    if not isinstance(meta, dict):
        return None
    target = meta.get(METADATA_KEY)
    if isinstance(target, dict) and target.get("chain") and target.get("address"):
        return target
    return None


def _is_canonical(chain: str, address: Optional[str]) -> bool:
    """USDC / wrapped native on *chain* (the registry's canonical pins)."""
    if not address:
        return False
    from core.wallet.addresses import same_address
    from core.wallet.tokens import CANONICAL_TOKENS
    return any(c == chain and same_address(a, address) for (c, a) in CANONICAL_TOKENS)


def acquisition_refusal(execution_context, *, chain: str, token_out: Optional[str],
                        token_in: Optional[str] = None, native_in: bool = False,
                        what: str = "buy") -> Optional[str]:
    """None when this acquisition is allowed under the run's target.

    The rule (068, decided after the third review): under a declared target the
    run acquires NO non-canonical token other than the target, on any chain.

    * the target on the target chain — allowed;
    * a CANONICAL asset (the chain's USDC or wrapped native) — allowed on any
      chain, whatever is sold for it: moving working capital (USDC -> WETH, a
      sell into the quote asset) acquires nothing the target restricts;
    * anything else, including ``token_out=None`` (an LP position, a
      deployment: an acquisition with no single token) — refused.

    ``token_in``/``native_in`` are kept for call-site compatibility; the rule
    no longer depends on what is sold.
    """
    target = target_from_context(execution_context)
    if target is None:
        return None
    from core.wallet.addresses import same_address
    chain = str(chain or "").strip().lower()
    if token_out is not None and _is_canonical(chain, token_out):
        return None
    want = target["address"]
    if token_out is not None and chain == target["chain"] and same_address(token_out, want):
        return None
    got = token_out or "a position with no single token"
    return (f"refused: this run declares its target token as {want} on "
            f"{target['chain']} ({PAYLOAD_KEY} in the job), and this {what} "
            f"acquires {got} on {chain}. A run with a declared target acquires "
            f"no token but that contract; it may still move working capital "
            f"between USDC, the wrapped native and native. Nothing was "
            f"broadcast.")


_TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _net_fungible_moves(deltas, holder: str) -> Dict[str, int]:
    """Signed NET movement per fungible contract for *holder* in the simulation.

    A MEASURED balance read (``token_deltas``) is authoritative for its token.
    For every other token the simulated log is summed: each 3-topic
    ``Transfer`` to the holder adds, each from the holder subtracts. A token
    that came in and went out again — a refund inside a sell — nets to <= 0 and
    is not an acquisition (068 R3-2).
    """
    measured = {str(t).lower(): int(moved or 0)
                for t, moved in (getattr(deltas, "token_deltas", None) or {}).items()}
    logged: Dict[str, int] = {}
    word = str(holder or "")[2:].lower().rjust(64, "0")
    for log in getattr(deltas, "logs", None) or ():
        try:
            topics = [str(t).lower() for t in (log.get("topics") or ())]
            if len(topics) != 3 or topics[0] != _TOPIC_TRANSFER:
                continue
            data = str(log.get("data") or "0x")
            amount = int(data, 16) if data not in ("", "0x") else 0
            contract = str(log.get("address") or "").lower()
            if not contract or not amount:
                continue
            if topics[2][2:] == word:
                logged[contract] = logged.get(contract, 0) + amount
            if topics[1][2:] == word:
                logged[contract] = logged.get(contract, 0) - amount
        except Exception:
            continue
    net = dict(logged)
    net.update(measured)
    return net


def net_inflow_refusal(execution_context, *, chain: str,
                       net_moves: Dict[str, int]) -> Optional[str]:
    """Refusal when any token with a POSITIVE net movement is neither the
    target nor canonical. Chain-family neutral: the EVM backstop and
    ``solana_swap`` (whose simulation measures every mint of ours it touched)
    both call it."""
    target = target_from_context(execution_context)
    if target is None:
        return None
    from core.wallet.addresses import same_address
    chain = str(chain or "").strip().lower()
    for contract, moved in sorted(net_moves.items()):
        if int(moved or 0) <= 0 or _is_canonical(chain, contract):
            continue
        if chain == target["chain"] and same_address(contract, target["address"]):
            continue
        return (f"refused: this run declares its target token as {target['address']} "
                f"on {target['chain']}, and the simulation shows it receiving "
                f"{contract} on {chain} (net +{int(moved)} raw). A run with a "
                f"declared target acquires no token but that contract. Nothing "
                f"was broadcast.")
    return None


def simulated_acquisition_refusal(execution_context, *, chain: str, deltas,
                                  holder: str, risk_reducing: bool = False) -> Optional[str]:
    """The guard-level backstop (068 N1, R3-1/R3-2): what the SIMULATION shows
    arriving, whatever verb built the transaction.

    Under a declared target, after simulation:

    * a RISK-REDUCING intent (removing liquidity, collecting LP fees, a revoke,
      a claim of what is owed) is exempt — it withdraws capital the wallet
      already has a claim on, it does not acquire;
    * a token with a POSITIVE net movement must be the target or canonical
      (a refund that nets to <= 0 is not an acquisition);
    * an NFT inflow is refused unless it comes from the target contract.
    """
    target = target_from_context(execution_context)
    if target is None or risk_reducing:
        return None
    from core.wallet.addresses import same_address
    chain = str(chain or "").strip().lower()
    # 068 R4-2: net the QUANTITY per (contract, standard, token id) — an
    # ERC-721 outflow never cancels an ERC-1155 inflow of the same id. An ERC-1155 send of
    # 1 unit with a receipt of 2 keeps +1 — "the id also went out" is not
    # "nothing was kept". ERC-721 moves count as 1.
    def _qty(amount) -> int:
        try:
            return int(amount) if amount is not None else 1
        except (TypeError, ValueError):
            return 1
    net_nft: dict = {}
    shown: dict = {}
    for (contract, standard, _cp, token_id, amount) in (
            getattr(deltas, "holder_nft_in", ()) or ()):
        key = (str(contract).lower(), str(standard).lower(), str(token_id))
        net_nft[key] = net_nft.get(key, 0) + _qty(amount)
        shown[key] = (contract, standard, token_id)
    for (contract, standard, _cp, token_id, amount) in (
            getattr(deltas, "holder_nft_out", ()) or ()):
        key = (str(contract).lower(), str(standard).lower(), str(token_id))
        net_nft[key] = net_nft.get(key, 0) - _qty(amount)
    for key, kept in net_nft.items():
        if kept <= 0 or key not in shown:
            continue  # nothing kept (sent at least as much as received)
        contract, standard, token_id = shown[key]
        if not (chain == target["chain"] and same_address(contract, target["address"])):
            return (f"refused: this run declares its target token as "
                    f"{target['address']} on {target['chain']}, and the simulation "
                    f"shows it receiving {standard} {contract} #{token_id}. A run "
                    f"with a declared target acquires no token but that contract. "
                    f"Nothing was broadcast.")
    return net_inflow_refusal(execution_context, chain=chain,
                              net_moves=_net_fungible_moves(deltas, holder))


def target_refusal(execution_context, *, chain: str, token_out: str,
                   token_in: Optional[str] = None, native_in: bool = False) -> Optional[str]:
    """Back-compat name for ``acquisition_refusal`` on a swap."""
    return acquisition_refusal(execution_context, chain=chain, token_out=token_out,
                               token_in=token_in, native_in=native_in)


def inherit_target(execution_context, requested: Any) -> Optional[Dict[str, str]]:
    """The target a job CREATED by this run must carry (068 B4).

    A target-bound run may not escape its restriction by scheduling work: the
    child inherits the run's target, and may only restate the SAME target. A
    different target (or an attempt to clear it) raises ``ValueError``.
    """
    from core.wallet.addresses import same_address
    run = target_from_context(execution_context)
    req = normalize_target(requested)
    if run is None:
        return req
    if req is None:
        # The CHILD's own authorship decides whether it is trust; the run's
        # stamp never travels into a stored payload.
        return {"chain": run["chain"], "address": run["address"]}
    if req["chain"] == run["chain"] and same_address(req["address"], run["address"]):
        return req
    raise ValueError(
        f"this run is bound to target {run['address']} on {run['chain']}; work "
        f"it creates may only carry the same target, not {req['address']} on "
        f"{req['chain']}")
