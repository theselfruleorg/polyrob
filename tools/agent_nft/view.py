"""The account view — what a buyer or an owner must read about an agent NFT (C20, core-owned).

Moved from the agent-NFT package (``polyrob_drop/inspect.py``) with its generic half: it works
for ANY pinned collection. Order is part of the contract: OPEN APPROVALS FIRST (they survive a
sale and ``state()`` does not see them), with their COVERAGE, then the named purchase checks,
then owner, lock, state, identity, native balance. Every value that could not be read renders
``unreadable`` — never 0, never empty (an unread approval table is NOT an empty one).

All reads are taken at ONE block (read once, then every ``latest`` is rewritten to it), so owner,
lock and approvals describe the same chain state. There is no ``safe_to_buy``: named checks, each
``ok`` / ``FAIL`` / ``unknown`` / ``note``.

What stays in the package: a collection's own lines (the face, the reveal state) through the
optional hook ``verbs.extend_view(view, rpc) -> list[str]``, appended to the render.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.wallet import abi, erc6551

UNREADABLE = "unreadable"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OK, FAIL, UNKNOWN, NOTE = "ok", "FAIL", "unknown", "note"


class ViewError(RuntimeError):
    """The view cannot be assembled (unpinned collection, changed code, no block)."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str      # ok | FAIL | unknown | note
    detail: str


@dataclass
class AccountView:
    chain: str
    chain_id: int
    collection: str
    token_id: int
    account: str
    block: Any = UNREADABLE               # the ONE block every read was taken at
    deploy_block: int = 0
    owner: Any = UNREADABLE
    owner_nested: Any = UNREADABLE        # True when the owner is itself a token-bound account
    implementation_ok: Any = UNREADABLE
    approvals: Any = UNREADABLE           # list[OpenApproval] or UNREADABLE
    coverage: Dict[str, Any] = field(default_factory=dict)
    locked: Any = UNREADABLE
    locked_until: Any = UNREADABLE
    state: Any = UNREADABLE
    identity_count: Any = UNREADABLE
    native_wei: Any = UNREADABLE
    treasury: Optional[str] = None        # this instance's treasury, when the caller named it
    owned_by_treasury: Any = UNREADABLE   # True when the treasury is the NFT's current owner
    notes: List[str] = field(default_factory=list)

    @property
    def coverage_complete(self) -> bool:
        return self.coverage.get("status") == "complete"

    def checks(self, *, now: Optional[float] = None) -> List[Check]:
        """The explicit purchase checks that replace ``safe_to_buy``."""
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        out: List[Check] = []
        if self.approvals == UNREADABLE:
            out.append(Check("approvals table complete & empty", UNKNOWN,
                             "the approval scan failed — an unread table is not an empty one"))
        elif self.approvals:
            out.append(Check("approvals table complete & empty", FAIL,
                             f"{len(self.approvals)} open approval(s); they survive a sale"))
        elif not self.coverage_complete:
            out.append(Check("approvals table complete & empty", UNKNOWN,
                             "no approval found, but the scan is INCOMPLETE: "
                             + "; ".join(self.coverage.get("gaps") or ["coverage unknown"])))
        else:
            out.append(Check("approvals table complete & empty", OK,
                             f"none, blocks {self.coverage['from_block']}..{self.coverage['to_block']}"))
        if self.locked == UNREADABLE:
            out.append(Check("locked", UNKNOWN, "isLocked() unreadable"))
        elif self.locked:
            out.append(Check("locked", OK, "the account cannot execute until lockedUntil"))
        else:
            out.append(Check("locked", FAIL, "not locked: the current owner can still move the account's "
                                            "assets before your purchase lands"))
        if self.locked_until == UNREADABLE:
            out.append(Check("lockedUntil", UNKNOWN, "lockedUntil() unreadable"))
        elif int(self.locked_until) > now:
            days = (int(self.locked_until) - now) / 86400
            out.append(Check("lockedUntil", NOTE, f"{_iso(self.locked_until)} (in {days:.1f} d): the lock "
                             f"survives the sale and blocks the buyer's execute until then"))
        else:
            out.append(Check("lockedUntil", NOTE, f"{int(self.locked_until)} (past or unset)"))
        if self.treasury:
            if self.owned_by_treasury == UNREADABLE:
                out.append(Check("treasury owns the NFT", UNKNOWN, "ownerOf() unreadable"))
            elif self.owned_by_treasury:
                out.append(Check("treasury owns the NFT", OK, "this instance may act through the account"))
            else:
                out.append(Check("treasury owns the NFT", FAIL, f"the owner is {self.owner}, not the "
                                 f"treasury {self.treasury}: the guard refuses every call through the account"))
        if self.implementation_ok is not True:
            out.append(Check("account is the pinned AccountV3 clone",
                             FAIL if self.implementation_ok is False else UNKNOWN, str(self.implementation_ok)))
        if self.owner_nested is True:
            out.append(Check("owner is a plain wallet", FAIL, "the owner is a token-bound account; nested "
                             "ownership is not supported"))
        return out


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).replace(microsecond=0).isoformat()
    except Exception:  # noqa: BLE001
        return str(ts)


def _try(view: AccountView, what: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a failed read is reported, never zeroed
        view.notes.append(f"{what}: {exc}")
        return UNREADABLE


def pinned_rpc(rpc, block: int):
    """``rpc`` with every ``latest`` block tag rewritten to *block* (one coherent snapshot)."""
    tag = hex(int(block))

    def call(method, params):
        if method in ("eth_call", "eth_getBalance", "eth_getCode", "eth_getStorageAt") \
                and params and params[-1] == "latest":
            params = list(params[:-1]) + [tag]
        return rpc(method, params)
    return call


def _logs(rpc, *, address: str, topics: list, from_block: int, to_block: int, step: int = 50_000) -> list:
    out, start = [], int(from_block)
    while start <= to_block:
        end = min(start + step - 1, to_block)
        got = rpc("eth_getLogs", [{"address": address, "topics": topics,
                                   "fromBlock": hex(start), "toBlock": hex(end)}])
        if not isinstance(got, list):
            raise ViewError(f"eth_getLogs {start}-{end} returned no list")
        out.extend(got)
        start = end + 1
    return sorted(out, key=lambda x: (int(x["blockNumber"], 16), int(x.get("logIndex", "0x0"), 16)))


def identity_balance(rpc, chain: str, account: str) -> int:
    from core.wallet import erc8004
    registry = erc8004.resolve_identity_registry(chain)
    raw = rpc("eth_call", [{"to": registry, "data": abi.encode_call(
        "balanceOf", [{"type": "address"}], [account])}, "latest"])
    return int(abi.decode([{"type": "uint256"}], raw)[0])


def identity_agent_id(rpc, chain: str, account: str, from_block: int, to_block: int) -> Optional[int]:
    """The ERC-8004 agentId the account holds, or None when it holds none.

    The reference identity registries are not ERC721Enumerable, so the id comes from the
    registry's ``Transfer(_, account, id)`` logs from *from_block* (the collection's deploy block),
    each confirmed live with ``ownerOf``. Raises :class:`ViewError` when the logs and ``balanceOf``
    disagree (an unnamed identity is never read as "none")."""
    from core.wallet import erc8004
    registry = erc8004.resolve_identity_registry(chain)
    held = identity_balance(rpc, chain, account)
    if held == 0:
        return None
    to_topic = "0x" + "0" * 24 + str(account).lower().removeprefix("0x")
    rows = _logs(rpc, address=registry, topics=[TOPIC_TRANSFER, None, to_topic], from_block=from_block,
                 to_block=to_block)
    ids = sorted({int(r["topics"][3], 16) for r in rows if len(r.get("topics") or []) == 4})
    live = []
    for i in ids:
        raw = rpc("eth_call", [{"to": registry, "data": abi.encode_call(
            "ownerOf", [{"type": "uint256"}], [i])}, "latest"])
        if ("0x" + str(raw)[-40:]).lower() == str(account).lower():
            live.append(i)
    if len(live) != held:
        raise ViewError(f"the account holds {held} ERC-8004 identit{'y' if held == 1 else 'ies'} but the "
                        f"registry's Transfer logs from block {from_block} name {live or 'none'} — refusing "
                        f"to guess the agentId")
    return live[0] if held == 1 else None


def approval_scan_gaps(from_block: int, deploy_block: int, to_block: Optional[int] = None,
                       head: Optional[int] = None) -> List[str]:
    """Why an approval scan cannot be called complete. Empty list = complete."""
    gaps = []
    if int(from_block) > int(deploy_block):
        gaps.append(f"the scan starts at block {from_block}, after the collection's deploy block "
                    f"{deploy_block}")
    if to_block is not None and head is not None and int(to_block) < int(head):
        gaps.append(f"the scan ends at block {to_block}, before the head block {head}")
    kinds = tuple(erc6551.APPROVAL_KINDS)
    for need in ("permit2", "erc6909", "erc6909_operator"):
        if need not in kinds:
            gaps.append(f"{need} approvals are not scanned")
    return gaps


def _is_tokenbound(rpc, address: str) -> bool:
    try:
        erc6551.read_implementation(rpc, address)
        return True
    except erc6551.Erc6551Error:
        return False


def resolve_target(target: str, default_chain: str = "robinhood"):
    """``'<chain>:<collection>/<id>'`` | ``'<chain>:<id>'`` | ``'<id>'`` → ``(chain, profile, id)``.
    The collection must be pinned on that chain (one pinned collection may be left unnamed)."""
    t = str(target).strip()
    chain = default_chain
    if ":" in t:
        chain, t = t.split(":", 1)
    collection = None
    if "/" in t:
        collection, t = t.rsplit("/", 1)
    try:
        token_id = int(t)
    except ValueError:
        raise ViewError(f"not an NFT reference: {target!r} (use <chain>:<collection>/<id>)") from None
    return (chain, *_profile(chain, collection), token_id)


def _profile(chain: str, collection: Optional[str]):
    from core.wallet import collection_registry
    try:
        chain_id = collection_registry.chain_id_of(chain)
        profiles = collection_registry.profiles_on(chain_id)
    except Exception as exc:  # noqa: BLE001
        raise ViewError(f"the owner's collection registry cannot be trusted ({exc})") from exc
    if collection:
        p = next((p for p in profiles if p.address == str(collection).lower()), None)
        if p is None:
            raise ViewError(f"{collection} is not a pinned collection on {chain}")
        return (p,)
    if len(profiles) != 1:
        raise ViewError(f"{len(profiles)} collections are pinned on {chain}: name one as "
                        f"<chain>:<collection>/<id>")
    return (profiles[0],)


def build_view(rpc, chain: str, profile, token_id: int, *,
               treasury: Optional[str] = None) -> AccountView:
    """Every read at ONE block; the approval scan always starts at the collection's deploy block."""
    from core.wallet import collection_registry
    why = collection_registry.runtime_refusal(rpc, profile)
    if why:
        raise ViewError(why)
    erc6551.verify(rpc)
    version = profile.accounts[0]
    account = erc6551.account_address(profile.chain_id, profile.address, int(token_id),
                                      salt=version.salt, implementation=version.implementation)
    v = AccountView(chain=chain, chain_id=profile.chain_id, collection=profile.address,
                    token_id=int(token_id), account=account, deploy_block=int(profile.deploy_block),
                    treasury=treasury.lower() if treasury else None)
    v.block = _try(v, "block number", lambda: int(rpc("eth_blockNumber", []), 16))
    if v.block == UNREADABLE:
        raise ViewError("could not read the block number — refusing to assemble a view")
    r = pinned_rpc(rpc, v.block)

    def _owner():
        raw = r("eth_call", [{"to": profile.address, "data": abi.encode_call(
            "ownerOf", [{"type": "uint256"}], [int(token_id)])}, "latest"])
        return erc6551._checksum(bytes.fromhex(str(raw)[-40:]))
    v.owner = _try(v, "ownerOf", _owner)
    v.implementation_ok = _try(v, "implementation", lambda: erc6551.read_implementation(
        r, account).lower() == str(version.implementation).lower())
    v.approvals = _try(v, "open approvals", lambda: erc6551.open_approvals(
        r, account, profile.deploy_block, to_block=v.block))
    gaps = approval_scan_gaps(profile.deploy_block, profile.deploy_block, to_block=v.block, head=v.block)
    if v.approvals == UNREADABLE:
        gaps = ["the scan failed"] + gaps
    v.coverage = {"status": "incomplete" if gaps else "complete", "from_block": profile.deploy_block,
                  "to_block": v.block, "gaps": gaps}
    v.locked = _try(v, "isLocked", lambda: erc6551.read_is_locked(r, account))
    v.locked_until = _try(v, "lockedUntil", lambda: erc6551.read_locked_until(r, account))
    v.state = _try(v, "state", lambda: erc6551.read_state(r, account))
    v.identity_count = _try(v, "identity", lambda: identity_balance(r, chain, account))
    v.native_wei = _try(v, "native balance", lambda: int(r("eth_getBalance", [account, "latest"]), 16))
    if v.owner != UNREADABLE:
        v.owner_nested = _try(v, "owner code", lambda: _is_tokenbound(r, v.owner))
        if treasury:
            v.owned_by_treasury = str(v.owner).lower() == treasury.lower()
    return v


_KIND_TEXT = {
    "erc721": lambda a: f"token #{a.token_id}",
    "operator": lambda a: "ApprovalForAll (every token)",
    "erc20": lambda a: f"allowance {a.amount}",
    "permit2": lambda a: f"Permit2 allowance {a.amount} (held by Permit2), expires {_iso(a.expiration)}",
    "erc6909": lambda a: f"ERC-6909 allowance {a.amount} (id {a.token_id})",
    "erc6909_operator": lambda a: "ERC-6909 operator (every id)",
}


def render(v: AccountView, *, now: Optional[float] = None, extra: List[str] = ()) -> str:
    out = [f"NFT #{v.token_id} on {v.chain} ({v.collection})", f"  account:  {v.account}",
           f"  block:    {v.block} (every value below was read at this block)", ""]
    cov = v.coverage or {}
    cov_line = (f"coverage {cov.get('status', 'unknown')}, blocks {cov.get('from_block')}..{cov.get('to_block')}"
                + (" — " + "; ".join(cov["gaps"]) if cov.get("gaps") else ""))
    if v.approvals == UNREADABLE:
        out.append("⚠ OPEN APPROVALS: UNREADABLE — do not treat this account as safe to buy")
    elif v.approvals:
        out.append(f"⚠ OPEN APPROVALS ({len(v.approvals)}) — NOT safe to buy until revoked:")
        for a in v.approvals:
            what = _KIND_TEXT.get(a.kind, lambda _a: a.kind)(a)
            out.append(f"    {a.kind:8} {a.contract} → {a.spender}  {what}"
                       + ("" if a.verified else "  (live read failed; kept)"))
    elif v.coverage_complete:
        out.append("  open approvals: none found (log scan + live confirmation)")
    else:
        out.append("  open approvals: none found, but the table is INCOMPLETE (not \"empty\")")
    out.append(f"  {cov_line}")
    out.append("")
    out.append("CHECKS")
    for c in v.checks(now=now):
        out.append(f"  [{c.status:7}] {c.name}: {c.detail}")
    out.append("")
    out.append(f"  owner:     {v.owner}")
    out.append(f"  clone:     {'AccountV3 (pinned)' if v.implementation_ok is True else v.implementation_ok}")
    out.append(f"  locked:    {v.locked}")
    out.append(f"  lockedUntil: {v.locked_until}"
               + (f" ({_iso(v.locked_until)})" if isinstance(v.locked_until, int) and v.locked_until else ""))
    out.append(f"  state():   {v.state}")
    out.append(f"  identity:  {v.identity_count} ERC-8004 token(s)")
    native = v.native_wei if v.native_wei == UNREADABLE else f"{v.native_wei / 1e18:.6f} ETH"
    out.append(f"  native:    {native}")
    out.extend(extra)
    if v.notes:
        out.append("  unreadable: " + "; ".join(v.notes))
    return "\n".join(out)
