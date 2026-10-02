"""The Python half of the injected wallet — where the policy actually is (042).

The page asks; this decides. Three families, and the split is the whole design:

* **READS** are forwarded to the chain's pinned RPC. They move nothing, so there
  is nothing to gate. The method allowlist exists so an unknown method fails
  loudly rather than being tunnelled somewhere.
* **``eth_sendTransaction``** is NOT signed because the page asked. It is turned
  into a ``TxIntent`` bounded by the ENVELOPE the agent declared when it armed
  the session, and put through ``tx_guard.authorize`` — the same choke point
  every other money verb goes through, with the same caps, the same daily cap
  and the same owner queue above the ceiling.
* **SIGNATURES are refused, always.** From ``tx_guard``'s own docstring: *"A
  signature is not a transaction. An EIP-2612/Permit2 payload is signed and
  submitted by someone else later, so it never reaches this function. That is
  why ``Signer.sign_typed_data`` is kept off the money path."* A permit is a
  standing claim on the wallet that no simulation can catch, granted by a page
  we do not control. A dapp that requires one is a dapp this rail cannot use,
  and it is told so in those words.

⚠️ What makes this SAFER than the generic ``defi_call`` verb, not more dangerous:
there, the declaration and the calldata have the same author (the model), which
``tx_guard`` records as the bound it cannot enforce. Here the agent declares the
envelope and the PAGE supplies the calldata, so the guard is adjudicating
between two parties instead of checking a claim against itself.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Read-only JSON-RPC the page may have answered from the pinned endpoint. An
#: allowlist, not a denylist: a method nobody vetted is refused, because the
#: cost of that is a dapp that does not work and the cost of the reverse is a
#: method that moves something.
READ_METHODS = frozenset({
    "eth_blockNumber", "eth_call", "eth_chainId", "eth_estimateGas",
    "eth_feeHistory", "eth_gasPrice", "eth_getBalance", "eth_getBlockByHash",
    "eth_getBlockByNumber", "eth_getCode", "eth_getLogs",
    "eth_getStorageAt", "eth_getTransactionByHash", "eth_getTransactionCount",
    "eth_getTransactionReceipt", "eth_maxPriorityFeePerGas", "net_version",
    "web3_clientVersion",
})

#: Every signing method, refused with one explanation.
SIGN_METHODS = frozenset({
    "personal_sign", "eth_sign", "eth_signTypedData", "eth_signTypedData_v1",
    "eth_signTypedData_v3", "eth_signTypedData_v4", "eth_signTransaction",
})

#: EIP-1193 error codes.
USER_REJECTED = 4001
UNAUTHORIZED = 4100
UNSUPPORTED_METHOD = 4200
CHAIN_NOT_ADDED = 4902


@dataclass
class Envelope:
    """What the AGENT authorized when it armed the session.

    Every field bounds the page, and the page cannot widen any of them.
    """
    chain: str
    max_spend_usd: float
    #: Total USD the whole session may spend, across every transaction the page
    #: asks for. Without it, a $5-per-transaction ceiling is a $5-per-CLICK
    #: ceiling, and a page can click as often as it likes.
    session_budget_usd: float
    #: Contracts the page may send transactions TO. Empty = any contract on the
    #: chain, which is the wide (and honest) default for exploring a dapp.
    allow_contracts: Tuple[str, ...] = ()
    #: Seconds a transaction may wait on an owner tap before the page is told
    #: the user rejected it. A page promise cannot hang forever.
    approval_timeout_sec: float = 300.0
    revoked: bool = False
    spent_usd: float = 0.0
    sent: List[Dict[str, Any]] = field(default_factory=list)
    refused: List[Dict[str, Any]] = field(default_factory=list)

    def remaining_usd(self) -> float:
        return max(0.0, self.session_budget_usd - self.spent_usd)


def envelope_snapshot(env: "Envelope", address: str) -> Dict[str, Any]:
    """A JSON-able snapshot of the live envelope for the durable store (043 A37).

    ``core`` owns the store bytes but may not import this tier, so the
    Envelope→dict serialization lives here. ``address`` rides along because a
    persisted record read after a restart has no live wallet to ask for it.
    """
    return {
        "chain": env.chain,
        "address": str(address or ""),
        "max_spend_usd": env.max_spend_usd,
        "session_budget_usd": env.session_budget_usd,
        "allow_contracts": list(env.allow_contracts or ()),
        "approval_timeout_sec": env.approval_timeout_sec,
        "revoked": bool(env.revoked),
        "spent_usd": env.spent_usd,
        "sent": list(env.sent or ()),
        "refused": list(env.refused or ()),
    }


def origin_of(url: Optional[str]) -> Optional[str]:
    """The web origin of ``url`` in ``location.origin`` form, or None.

    ``scheme://host[:port]`` — the host in its wire (IDNA) form, the port only
    when it is not the scheme default — so the Python check and the page-side
    ``location.origin`` gate compare the same string. Anything that is not an
    anonymous http(s) URL (``about:blank``, ``data:``, userinfo) has no origin
    a wallet may be armed for.
    """
    from urllib.parse import urlsplit

    from core.security.pinned_resolver import url_host_key
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https") or parts.username or parts.password:
            return None
        host = url_host_key(str(url))
        if not host:
            return None
        port = parts.port
    except Exception:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def calldata_words(data: str) -> Dict[str, Any]:
    """The full calldata as the owner should see it: selector + 32-byte words.

    Not an ABI decode (the page's ABI is unknown), but every argument word is
    shown, never a 10-character selector alone.
    """
    body = (data or "0x")[10:] if len(data or "") >= 10 else ""
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    return {"selector": (data or "0x")[:10], "args": words}


def _error(code: int, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


def _result(value: Any) -> str:
    return json.dumps({"result": value})


def _hex_int(raw) -> int:
    if raw is None or raw == "0x" or raw == "":
        return 0
    if isinstance(raw, int):
        return raw
    return int(str(raw), 16)


class WalletBridge:
    """One armed dapp session. Created by ``dapp_connect``, revoked by ``dapp_disconnect``."""

    def __init__(self, *, envelope: Envelope, wallet, execution_context=None,
                 container=None, rail_factory=None, guard_fn=None,
                 price_fn=None, rpc_fn=None, approver=None, persist_fn=None,
                 armed_origin: Optional[str] = None,
                 taint_probe: Optional[Callable[[], bool]] = None,
                 turn_kind_probe: Optional[Callable[[], Optional[str]]] = None):
        self.envelope = envelope
        #: The ONE origin this envelope was armed for (from dapp_connect's url).
        #: A request from any other origin, a sub-frame or another tab is
        #: refused before it is even parsed (H03b). None = never armed = every
        #: request refused.
        self.armed_origin = armed_origin
        #: Is the SESSION correspondent-tainted right now? The page can ask for
        #: a transaction long after the arming turn, including while a third
        #: party's message is in context. A probe that raises reads as tainted.
        self._taint_probe = taint_probe
        #: The LIVE turn kind of the session (CR-L18) — ``_forged_turn_kind``
        #: read at request time, not the arming turn's frozen context. A page
        #: asks long after arming; a later self-wake / delegation-result /
        #: group turn must be judged as what it is. A probe that raises refuses.
        self._turn_kind_probe = turn_kind_probe
        #: Set by ``dapp_connect`` before the binding is exposed: until
        #: ``attach_page`` binds the tab, no transaction may be signed (the
        #: window between ``expose_binding`` and ``attach_page``).
        self.require_attached_page = False
        #: The page the wallet was armed on. When known, only its main frame
        #: may call the binding, and leaving the origin revokes the envelope.
        self._page = None
        self._refused_origins: set = set()
        self._wallet = wallet
        self._ctx = execution_context
        self._container = container
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn
        self._rpc_fn = rpc_fn
        self._approver = approver
        #: Called after every envelope mutation (spend, refusal) so the durable
        #: store (043 A37) mirrors what the wallet DID and ``dapp_status`` can
        #: report it after a restart. Fail-open — persistence is a reporting
        #: nicety and must never break a spend.
        self._persist_fn = persist_fn

    # -- plumbing ---------------------------------------------------------

    @property
    def address(self) -> str:
        return self._wallet.operational_signer().address

    def chain_id_hex(self) -> str:
        from core.wallet import chains
        return hex(int(chains.get(self.envelope.chain).chain_id))

    def _rpc(self, method: str, params: list):
        if self._rpc_fn is not None:
            return self._rpc_fn(self.envelope.chain, method, params)
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        return _rpc(rpc_url_for_chain(self.envelope.chain), method, params,
                    timeout=10.0)

    # -- the binding ------------------------------------------------------

    # -- where a request may come from (H03b) ------------------------------

    def attach_page(self, page) -> None:
        """Bind the envelope to ``page`` and revoke it when the page leaves the
        armed origin. The binding and the init script are CONTEXT-wide, so
        without this a later page — or the same tab after a navigation — would
        inherit a live wallet it was never armed for."""
        self._page = page
        try:
            page.on("framenavigated", self._on_frame_navigated)
        except Exception as exc:
            logger.warning("dapp bridge: cannot watch navigation (%s)", exc)
            raise

    def _on_frame_navigated(self, frame) -> None:
        page = self._page
        try:
            if page is None or frame is not page.main_frame:
                return
            where = origin_of(getattr(frame, "url", None))
        except Exception:
            where = None
        if where == self.armed_origin or self.envelope.revoked:
            return
        self.envelope.revoked = True
        self._refuse(UNAUTHORIZED, "navigated-away", (
            f"the page left {self.armed_origin} for {where or 'a non-web page'}; "
            f"the wallet was revoked. Re-arm with dapp_connect on the new origin."))

    def _source_refusal(self, source) -> Optional[str]:
        """Why a binding call from ``source`` is refused, or None."""
        if not self.armed_origin:
            return "this wallet session is not bound to any origin"
        if not isinstance(source, dict):
            return "the request carries no frame"
        frame, page = source.get("frame"), source.get("page")
        if frame is None or page is None:
            return "the request carries no frame"
        try:
            if frame is not page.main_frame:
                return "requests from an embedded frame are refused"
            if self._page is not None and page is not self._page:
                return "requests from another tab are refused"
            where = origin_of(frame.url)
        except Exception:
            return "the request's frame could not be identified"
        if where != self.armed_origin:
            return (f"this wallet is armed for {self.armed_origin}, not "
                    f"{where or 'a non-web page'}")
        return None

    def _tainted(self) -> bool:
        probe = self._taint_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            logger.warning("dapp bridge: taint probe failed; treating as tainted",
                           exc_info=True)
            return True

    async def handle(self, source, raw) -> str:
        """Playwright binding entry point. NEVER raises into the page."""
        why = self._source_refusal(source)
        if why is not None:
            # Recorded once per offending origin, so a hostile frame cannot
            # flood the envelope (and its durable mirror) with refusals.
            key = why
            if key not in self._refused_origins and len(self._refused_origins) < 32:
                self._refused_origins.add(key)
                return self._refuse(UNAUTHORIZED, "wrong-origin", why)
            return _error(UNAUTHORIZED, why)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            method = str(payload.get("method") or "")
            params = payload.get("params") or []
            return await self._dispatch(method, params)
        except Exception as exc:                      # pragma: no cover - defensive
            logger.warning("dapp bridge: %s", exc, exc_info=True)
            return _error(USER_REJECTED, f"the wallet could not answer: {exc}")

    def _durably_revoked(self) -> bool:
        """Has an OWNER revoked this session out of process (E9, 2026-09-21)?

        ``polyrob wallet dapp revoke`` / ``/dapp revoke`` flip the durable row;
        a bridge already armed in a running session must honour that on its
        next request, or the owner's revoke is a note, not a stop. Mirrored
        into the in-memory envelope once seen. An unreadable store reads as
        "not revoked" — the in-memory flag is the primary and this is the
        cross-process backstop; a read never creates the store.
        """
        session_id = getattr(self._ctx, "session_id", None)
        if not session_id:
            return False
        try:
            import os as _os

            from core.dapp_session_store import (
                default_dapp_session_store_path, get_dapp_session_store,
            )
            # ⚠️ `get_dapp_session_store()` runs `init_schema(..., mkdir=True)`,
            # so calling it blind CREATES the db. This is the read half of the
            # rail and it runs on EVERY page request, so an absent file must
            # stay absent — an arming WRITES the row first, so "no file" is
            # "nothing was ever armed here", never "unknown".
            if not _os.path.exists(default_dapp_session_store_path()):
                return False
            row = get_dapp_session_store().get(str(session_id))
        except Exception:
            logger.debug("dapp bridge: durable revoke flag unreadable", exc_info=True)
            return False
        if row is not None and bool(row.revoked):
            self.envelope.revoked = True
            return True
        return False

    async def _dispatch(self, method: str, params: list) -> str:
        if self.envelope.revoked or self._durably_revoked():
            return _error(UNAUTHORIZED, (
                "this wallet session was revoked — reconnect with "
                "dapp_connect before asking again"))

        if method in ("eth_accounts", "eth_requestAccounts"):
            return _result([self.address])
        if method == "eth_chainId":
            return _result(self.chain_id_hex())
        if method in ("wallet_getPermissions", "wallet_requestPermissions"):
            return _result([{"parentCapability": "eth_accounts"}])

        if method in SIGN_METHODS:
            return self._refuse(UNSUPPORTED_METHOD, "signature", (
                "this wallet does not produce off-chain signatures. A signature "
                "is not a transaction: a permit is submitted by someone else "
                "later, so no simulation can catch what it authorizes. Use a "
                "path that sends a transaction instead."))

        if method == "wallet_switchEthereumChain":
            return self._switch_chain(params)

        if method == "wallet_addEthereumChain":
            return _error(UNSUPPORTED_METHOD, (
                "chains are pinned in this wallet's registry and cannot be added "
                "from a page"))

        if method == "eth_sendTransaction":
            if self.require_attached_page and self._page is None:
                return self._refuse(USER_REJECTED, "not-attached", (
                    "the wallet is not yet bound to its tab; nothing can be "
                    "signed until dapp_connect finishes"))
            if self._tainted():
                return self._refuse(USER_REJECTED, "tainted", (
                    "this session has a third party's message in context "
                    "(correspondent-tainted); the wallet will not sign for a "
                    "page until the owner's next turn"))
            return await self._send_transaction(params)

        if method in READ_METHODS:
            try:
                return _result(self._rpc(method, list(params)))
            except Exception as exc:
                return _error(USER_REJECTED, f"rpc read failed: {exc}")

        return _error(UNSUPPORTED_METHOD, f"{method} is not supported by this wallet")

    # -- chain switching --------------------------------------------------

    def _switch_chain(self, params: list) -> str:
        from core.wallet import chains
        try:
            wanted = _hex_int((params or [{}])[0].get("chainId"))
        except Exception:
            return _error(USER_REJECTED, "malformed wallet_switchEthereumChain")
        if wanted == int(chains.get(self.envelope.chain).chain_id):
            return json.dumps({"result": None, "chainId": self.chain_id_hex()})
        for name in chains.names():
            row = chains.get(name)
            if int(getattr(row, "chain_id", 0) or 0) != wanted:
                continue
            # The ENVELOPE names a chain, and its budget was authorized for THAT
            # chain. Letting a page move the session elsewhere would spend an
            # authorization that was never given — so this refuses even when the
            # target chain is perfectly money-capable.
            return _error(CHAIN_NOT_ADDED, (
                f"this session is armed for {self.envelope.chain}, not {name}. "
                f"The agent must re-arm with dapp_connect(chain='{name}') — a "
                f"page cannot move a budget to a chain it was not granted on."))
        return _error(CHAIN_NOT_ADDED,
                      f"chain id {wanted} is not in this wallet's registry")

    # -- the money path ---------------------------------------------------
    async def _send_transaction(self, params: list) -> str:
        """Validate, then: attempt → (owner wait, UNLOCKED) → attempt again.

        ⚠️ The owner wait is deliberately OUTSIDE ``gate.reserve()``. That lock
        is the wallet's ONE process-wide reservation, and an owner tap can take
        an hour: holding it across the wait would block every other money verb
        — including a stop-loss exit on a position sliding down — for as long as
        the declared timeout. A page could freeze the whole treasury's ability
        to act. Every sibling verb waits outside the reservation
        (``bridge_verb``) or returns immediately (``wrap``); this does the
        former, and then RE-AUTHORIZES against a fresh simulation, because
        minutes have passed and the transaction the owner approved is the one
        that was priced then.
        """
        from core.wallet.broadcast.evm import EvmRail
        from core.wallet.tokens import normalize_address

        try:
            req = dict((params or [{}])[0] or {})
        except Exception:
            return _error(USER_REJECTED, "malformed eth_sendTransaction")

        to = req.get("to")
        if not to:
            return self._refuse(USER_REJECTED, "deploy-from-page", (
                "this wallet will not deploy a contract from a page. Use "
                "defi_trade.deploy_contract, where the produced bytecode is "
                "asserted before anything is signed."))
        try:
            to = normalize_address(str(to))
        except Exception as exc:
            return self._refuse(USER_REJECTED, "bad-to",
                                f"`to` is not a valid address ({exc})")

        allow = {a.lower() for a in self.envelope.allow_contracts}
        if allow and to.lower() not in allow:
            return self._refuse(USER_REJECTED, "not-allowlisted", (
                f"this session may only transact with "
                f"{', '.join(sorted(self.envelope.allow_contracts))} — the page "
                f"asked for {to}"))

        frm = req.get("from")
        if frm and str(frm).lower() != self.address.lower():
            return self._refuse(USER_REJECTED, "wrong-from", (
                f"the page asked to send from {frm}, which is not this wallet"))

        data = req.get("data") or req.get("input") or "0x"
        if not isinstance(data, str) or not data.startswith("0x"):
            return self._refuse(USER_REJECTED, "bad-calldata",
                                "calldata is not 0x-prefixed hex")
        try:
            value_wei = _hex_int(req.get("value") or 0)
        except Exception:
            return self._refuse(USER_REJECTED, "bad-value",
                                "value is not a hex quantity")

        from tools.defi.call_verb import typed_verb_only_refusal
        pinned_err = typed_verb_only_refusal(to)
        if pinned_err:
            return self._refuse(USER_REJECTED, "typed-verb-only", pinned_err)

        signer = self._wallet.operational_signer()
        gate = self._wallet.policy
        rail = (self._rail_factory or EvmRail)(chain=self.envelope.chain,
                                               signer=signer)

        reply, pending = await self._attempt(
            rail=rail, gate=gate, signer=signer, to=to, data=data,
            value_wei=value_wei)
        if reply is not None:
            return reply

        approved_usd = float(pending.amount_usd or 0.0)
        if not await self._await_owner(to, data, pending, value_wei=value_wei):
            return self._refuse(USER_REJECTED, "owner-declined", (
                f"{pending.reason} — and the owner did not approve it within "
                f"{self.envelope.approval_timeout_sec:.0f}s"))

        reply, pending = await self._attempt(
            rail=rail, gate=gate, signer=signer, to=to, data=data,
            value_wei=value_wei, owner_approved_usd=approved_usd)
        if reply is not None:
            return reply
        # Still only the ceiling in the way after re-pricing, and the re-priced
        # figure is outside what the owner agreed to.
        return self._refuse(USER_REJECTED, "reprice-after-approval", (
            f"the owner approved ${approved_usd:.4f}, but the same transaction "
            f"now prices at ${float(pending.amount_usd or 0.0):.4f} — that is "
            f"not what he approved. Ask again."))

    def _live_context(self):
        """The arming context with the LIVE turn kind stamped in (CR-L18).

        Returns ``(ctx, None)`` or ``(None, why)``. The more restrictive of the
        frozen and the live kind wins: a forged arming turn stays forged.

        Every page request is marked ``PAGE_REQUEST_KEY`` and carries no
        ``owner_grant``: the page, not the owner, asks for it, so the pause and
        the autonomous ceiling bind even when the owner armed the session in
        the owner's own turn. Above the ceiling the owner approves it here, per request
        (``_await_owner``).
        """
        from core.money.authority import PAGE_REQUEST_KEY
        from core.wallet.tx_guard import OWNER_GRANT_KEY
        probe = self._turn_kind_probe
        live = None
        if probe is not None:
            try:
                live = probe()
            except Exception as exc:
                return None, (f"the session's live turn could not be read ({exc}); "
                              f"the wallet will not sign for a page it cannot "
                              f"attribute to a genuine turn")
        if self._ctx is None:
            if probe is None:
                return None, None
            return None, ("no agent turn is attached to this wallet session; "
                          "failing closed")
        import copy
        frozen = dict(getattr(self._ctx, "metadata", None) or {})
        frozen.pop(OWNER_GRANT_KEY, None)
        ctx = copy.copy(self._ctx)
        try:
            ctx.metadata = {**frozen, "turn_kind": frozen.get("turn_kind") or live,
                            PAGE_REQUEST_KEY: True}
        except Exception as exc:
            return None, f"could not stamp the live turn ({exc}); failing closed"
        return ctx, None

    async def _attempt(self, *, rail, gate, signer, to, data, value_wei,
                       owner_approved_usd=None):
        """``(reply, pending)`` — exactly one is None.

        A ``reply`` is final (sent, or refused). A ``pending`` Decision means the
        ONLY thing in the way is the autonomous ceiling, and the caller should
        ask the owner — holding no lock.

        Everything from the budget read to ``gate.record`` happens inside ONE
        reservation, which is what that lock is for: a page can fire two
        eth_sendTransaction calls concurrently, and a check outside the lock
        lets both pass a nearly-exhausted budget and both spend against it.
        """
        from core.wallet import tx_guard
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        authorize = self._guard_fn or tx_guard.authorize

        from tools.defi.call_verb import intent_idempotency_key

        ctx, why = self._live_context()
        if why is not None:
            return self._refuse(USER_REJECTED, "turn-unknown", why), None

        # 068 N1: a page-built transaction cannot be classified as "acquires only
        # the target" — its effect is whatever the page encoded. A run bound to a
        # target_token never signs one.
        from core.wallet.buy_target import target_from_context
        _run_target = target_from_context(ctx) or target_from_context(self._ctx)
        if _run_target is not None:
            return self._refuse(USER_REJECTED, "target-bound", (
                f"this run declares its target token as {_run_target['address']} "
                f"on {_run_target['chain']}; a dapp transaction cannot be proven "
                f"to acquire only that contract, so it is refused. Use "
                f"defi_trade.swap (or call with receive_token = the target).")), None

        try:
            tx = rail.build_call(to=to, data=data, value=value_wei)
        except Exception as exc:
            return self._refuse(USER_REJECTED, "build-failed",
                                f"could not build the transaction: {exc}"), None
        idem = intent_idempotency_key(
            "dapp", chain=self.envelope.chain, to=to, data=data,
            value_wei=value_wei, tx=tx, execution_context=self._ctx)

        async with gate.reserve():
            remaining = self.envelope.remaining_usd()
            if remaining <= 0:
                return self._refuse(USER_REJECTED, "budget-exhausted", (
                    f"this dapp session's "
                    f"${self.envelope.session_budget_usd:.2f} budget is spent. "
                    f"The agent must re-arm it deliberately.")), None
            ceiling = min(self.envelope.max_spend_usd, remaining)

            # A page-supplied call declares a NATIVE outflow of exactly the value
            # it carries. Any token movement is UNDECLARED, and tx_guard refuses
            # it — the correct default for a dapp we are exploring. A page that
            # needs to move a token must first get an allowance through
            # `approve_token`, where the grant is declared, priced and capped.
            intent = tx_guard.TxIntent(
                chain=self.envelope.chain, token=None, to=to,
                amount_raw=value_wei, max_spend_usd=ceiling,
                idempotency_key=idem)

            decision = authorize(intent, tx, holder=signer.address, gate=gate,
                                 execution_context=ctx, tool_self=self,
                                 price_fn=self._price_fn,
                                 forged_fn=_is_forged_or_autonomous_turn,
                                 autonomous_ok_fn=_is_autonomous_goal_turn)

            if not decision.allowed:
                if decision.lane != "owner_queue":
                    return self._refuse(USER_REJECTED, "guard",
                                        decision.reason), None
                if owner_approved_usd is None:
                    return None, decision          # go ask, holding nothing
                # The owner lifted the ceiling — for a PRICE. A re-simulation
                # that costs materially more is not the transaction he approved.
                now_usd = float(decision.amount_usd or 0.0)
                if now_usd > owner_approved_usd + max(0.01, owner_approved_usd * 0.10):
                    return None, decision
                logger.info(
                    "dapp bridge: proceeding on the owner's approval "
                    "(approved $%.4f, re-priced $%.4f)", owner_approved_usd, now_usd)

            if decision.sim_gas_used:
                try:
                    tx = rail.size_gas(tx, decision.sim_gas_used)
                except Exception as exc:
                    return self._refuse(USER_REJECTED, "gas",
                                        f"refused at gas sizing: {exc}"), None

            spent = float(decision.amount_usd or 0.0)
            if spent > remaining:
                return self._refuse(USER_REJECTED, "budget-exhausted", (
                    f"this transaction prices at ${spent:.4f} and only "
                    f"${remaining:.4f} of the session budget is left")), None

            try:
                tx_hash = rail.sign_and_send(tx)
            except Exception as exc:
                return self._refuse(USER_REJECTED, "broadcast",
                                    f"broadcast failed: {exc}"), None

            self.envelope.spent_usd += spent
            self.envelope.sent.append({
                "tx": tx_hash, "to": to, "selector": data[:10],
                "usd": spent, "at": time.time()})
            gate.record(venue="defi", action="dapp_call", amount_usd=spent,
                        counterparty=to, idempotency_key=idem,
                        result_ref=tx_hash, chain=self.envelope.chain)
            self._persist()

        self._notify(to, data, decision, tx_hash)
        return _result(tx_hash), None

    async def _await_owner(self, to: str, data: str, decision, *,
                           value_wei: int = 0) -> bool:
        """Block the page's promise on the durable owner queue, with a deadline.

        The grant is keyed on the FULL call — a hash of the whole calldata and
        the value — not only the selector, so an approval for one call cannot
        be redeemed by a different call to the same function. The owner sees
        every argument word, not a truncated selector.
        """
        import asyncio

        approver = self._approver
        if approver is None:
            try:
                from tools.controller.approval_queue import OwnerQueueApprover
                approver = OwnerQueueApprover(
                    user_id=getattr(self._ctx, "user_id", None),
                    taint_probe=self._taint_probe)
            except Exception as exc:
                logger.warning("dapp bridge: no owner queue (%s)", exc)
                return False
        calldata_sha256 = hashlib.sha256(
            (data or "0x").lower().encode("ascii", "replace")).hexdigest()
        key = {"chain": self.envelope.chain, "to": to,
               "selector": data[:10], "usd": decision.amount_usd,
               "value_wei": str(int(value_wei or 0)),
               "calldata_sha256": calldata_sha256,
               "origin": self.armed_origin,
               "via": "dapp browser"}
        summary = dict(key)
        summary.update(calldata_words(data))
        try:
            return bool(await asyncio.wait_for(
                approver.request("dapp_browser_dapp_connect", summary,
                                 self._ctx, hash_params=key),
                timeout=self.envelope.approval_timeout_sec))
        except asyncio.TimeoutError:
            return False
        except Exception as exc:
            logger.warning("dapp bridge: owner approval failed (%s)", exc)
            return False

    def _refuse(self, code: int, kind: str, message: str) -> str:
        """Every refusal is RECORDED, so `dapp_status` can tell the agent why a
        page is not working instead of leaving it to guess from a blank screen."""
        self.envelope.refused.append({"kind": kind, "why": message,
                                      "at": time.time()})
        self._persist()
        return _error(code, message)

    def _persist(self) -> None:
        """Mirror the envelope into the durable store after a mutation (043 A37).
        Fail-open: a broken store must never break a spend or a refusal."""
        fn = self._persist_fn
        if fn is None:
            return
        try:
            fn(self)
        except Exception as exc:
            logger.debug("dapp bridge: persist failed (%s)", exc)

    def _notify(self, to: str, data: str, decision, tx_hash: str) -> None:
        """The owner hears about money that moved, exactly like every other verb."""
        try:
            from core.wallet import tx_notify
            used, limit = tx_notify.caps_from_gate(self._wallet.policy)
            tx_notify.notify_soon(
                self._container,
                getattr(self._ctx, "user_id", None),
                tx_notify.TxNotice(
                    verb="dapp", route=f"{self.envelope.chain}:{to}",
                    chain=self.envelope.chain,
                    amount_in=data[:10], usd=decision.amount_usd,
                    tx_ref=tx_hash, lane=decision.lane,
                    cap_used_usd=used, cap_limit_usd=limit,
                    detail="signed for a dapp page"),
                settled=False,
                session_id=getattr(self._ctx, "session_id", None))
        except Exception as exc:
            logger.debug("dapp bridge: notify failed (%s)", exc)
