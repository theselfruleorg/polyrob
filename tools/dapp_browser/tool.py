"""The ``dapp_browser`` tool — a wallet in the page, not a key in the page (042).

The gap this closes: the agent could read any dapp and drive any dapp's UI, and
every one of them was effectively read-only, because the page found no
``window.ethereum``. Connect buttons did nothing. That is most of DeFi, every
NFT mint, and every launchpad that never shipped an API.

``dapp_connect`` installs an EIP-1193 provider (plus the EIP-6963 announcement
modern dapps actually listen for) into the SESSION's existing browser context,
so every ordinary ``browser_*`` action keeps working — this adds a wallet, not a
second browser and not a second set of click verbs.

⚠️ ``dapp_connect`` IS the money verb, and it is on the money lanes for that
reason. The spend does not happen in an action the Controller can see: it
happens when the PAGE calls ``eth_sendTransaction``. Connecting a wallet to a
dapp with a declared budget is the act of authorization, so that is what is
gated, and every individual transaction is still simulated, asserted and capped
by ``tx_guard`` inside the bridge.

⚠️ This module deliberately does NOT ``from __future__ import annotations``.
"""
import logging
import types

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from tools.wallet_holder import WalletHolderMixin

logger = logging.getLogger(__name__)

FLAG = "DAPP_BROWSER_ENABLED"

_NULL_CONFIG = types.SimpleNamespace()


def dapp_browser_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


class ConnectParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., description=(
        "The dapp URL to open with the wallet attached. The page is loaded "
        "AFTER the provider is installed, which is what makes the dapp see it."))
    chain: str = Field("base", description=(
        "Chain the session is armed for. The page cannot move the budget to "
        "another chain — that needs a deliberate reconnect."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD ONE transaction from this page may spend."))
    session_budget_usd: float = Field(..., gt=0, description=(
        "The most USD the WHOLE session may spend across every transaction the "
        "page asks for. Without this a per-transaction ceiling is only a "
        "per-CLICK ceiling, and a page can click as often as it likes."))
    allow_contracts: str = Field("", description=(
        "Optional comma-separated contract addresses this page may transact "
        "with. Empty means any contract on the chain — the honest default for "
        "exploring a dapp, and the looser one."))
    approval_timeout_sec: float = Field(300.0, ge=10, le=3600, description=(
        "How long a transaction above the autonomous ceiling may wait on an "
        "owner tap before the page is told the request was rejected. A page "
        "promise cannot hang forever."))


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DappBrowserTool(WalletHolderMixin, BaseTool):
    """Arms, reports and revokes the injected wallet for one session."""

    def __init__(self, name: str = "dapp_browser", config=None, container=None, *,
                 wallet=None, rail_factory=None, guard_fn=None, price_fn=None,
                 rpc_fn=None, approver=None):
        super().__init__(name=name,
                         config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn
        self._rpc_fn = rpc_fn
        self._approver = approver
        #: One bridge per session id. A second connect on the same session
        #: REPLACES it, so a stale envelope can never keep spending.
        self._bridges = {}
        #: Durable envelope store (043 A37) so `dapp_status` survives a restart.
        #: Lazily resolved; None means persistence is unavailable, which never
        #: blocks a connect or a spend (the in-memory bridge is authoritative).
        self._store = None

    async def _initialize(self):
        return True

    # -- plumbing ---------------------------------------------------------

    def _price(self, chain, addr):
        if self._price_fn:
            return self._price_fn(chain, addr)
        # 071: the one read layer; a DISPUTED quote yields None.
        from tools.defi.price_sources import spend_price
        return spend_price(chain, addr)

    def _key(self, execution_context) -> str:
        return str(getattr(execution_context, "session_id", None) or "default")

    def _uid(self, execution_context) -> str:
        return str(getattr(execution_context, "user_id", None) or "")

    def _taint_probe_for(self, execution_context):
        """A probe for "is this session correspondent-tainted NOW?" (H03b).

        Reads the SAME orchestrator flag the correspondent gate reads
        (``_correspondent_tainted``), resolved per call because the page asks
        long after the arming turn. When no orchestrator can be resolved the
        probe answers TAINTED (CR-L18): a session whose state cannot be read
        cannot be proven clean, and the page is not the owner.
        """
        session_id = self._key(execution_context)

        def _probe() -> bool:
            from tools.ship_common import resolve_orchestrator
            orch = resolve_orchestrator(lambda: self.container, session_id)
            if orch is None:
                return True
            return bool(getattr(orch, "_correspondent_tainted", False))
        return _probe

    def _turn_kind_probe_for(self, execution_context):
        """The session's LIVE turn kind, read per page request (CR-L18).

        The same derivation ``step_execution._build_execution_context`` stamps
        into ``metadata["turn_kind"]``: the orchestrator's ``_forged_turn_kind``,
        else ``"group"`` for a public room. Raises when no orchestrator can be
        resolved, which the bridge turns into a refusal.
        """
        session_id = self._key(execution_context)

        def _probe():
            from tools.ship_common import resolve_orchestrator
            orch = resolve_orchestrator(lambda: self.container, session_id)
            if orch is None:
                raise RuntimeError("the session's orchestrator cannot be resolved")
            kind = getattr(orch, "_forged_turn_kind", None)
            if kind is None:
                from core.surfaces.room_policy import is_public_session
                if is_public_session(orch):
                    kind = "group"
            return kind
        return _probe

    def _get_store(self):
        """The durable dapp-session store, or None. Fail-open + LOUD — a broken
        store must never break a connect (the in-memory bridge is authoritative)."""
        if self._store is not None:
            return self._store
        try:
            from core.dapp_session_store import get_dapp_session_store
            self._store = get_dapp_session_store()
        except Exception as exc:
            logger.warning("dapp session store unavailable: %s", exc)
            self._store = None
        return self._store

    def _persist_bridge(self, wallet_bridge) -> None:
        """Mirror one bridge's live envelope into the durable store. Passed to
        `WalletBridge` as its persist hook and called on connect/disconnect."""
        store = self._get_store()
        if store is None:
            raise OSError("dapp budget store is unavailable")
        ctx = getattr(wallet_bridge, "_ctx", None)
        session_id = str(getattr(ctx, "session_id", None) or "default")
        user_id = str(getattr(ctx, "user_id", None) or "")
        from tools.dapp_browser.bridge import envelope_snapshot
        snap = envelope_snapshot(wallet_bridge.envelope, wallet_bridge.address)
        store.save(session_id, user_id, snap,
                   revoked=bool(wallet_bridge.envelope.revoked))

    def _persisted_status(self, execution_context) -> str:
        """A read-only render of a persisted session, or None when there is no
        row. Used after a restart, when the in-memory bridge is gone: the browser
        binding is gone with it, so this record CANNOT spend and says so."""
        store = self._get_store()
        if store is None:
            return "dapp session store is unavailable; saved status could not be read"
        try:
            row = store.get(self._key(execution_context),
                            user_id=self._uid(execution_context) or None)
        except Exception as exc:
            logger.warning("dapp session read failed: %s", exc)
            return "dapp session store is unavailable; saved status could not be read"
        if row is None:
            return None
        env = row.envelope or {}
        spent = float(env.get("spent_usd") or 0.0)
        budget = float(env.get("session_budget_usd") or 0.0)
        left = max(0.0, budget - spent)
        sent = env.get("sent") or []
        refused = env.get("refused") or []
        lines = [
            f"dapp wallet on {env.get('chain')} — saved record from a previous "
            f"run (not live this session; reconnect with dapp_connect to spend)"
            + (" — REVOKED" if row.revoked or env.get("revoked") else ""),
            f"  address:  {env.get('address') or 'unknown'}",
            f"  spent:    ${spent:.4f} of ${budget:.2f} (${left:.4f} left)",
            f"  per tx:   ${float(env.get('max_spend_usd') or 0.0):.2f}",
            f"  signed:   {len(sent)}",
        ]
        for item in sent[-5:]:
            lines.append(f"    {item.get('tx')} -> {item.get('to')} "
                         f"({item.get('selector')}, "
                         f"${float(item.get('usd') or 0.0):.4f})")
        lines.append(f"  refused:  {len(refused)}")
        for item in refused[-5:]:
            lines.append(f"    {item.get('kind')}: {item.get('why')}")
        return "\n".join(lines) + "\n"

    # -- verbs ------------------------------------------------------------

    @BaseTool.action(
        "Open a dapp with THIS AGENT'S OWN WALLET attached, so the page's "
        "Connect button works and its transactions can actually be signed. You "
        "declare the envelope: which chain, the most one transaction may spend, "
        "the most the whole session may spend, and optionally the only "
        "contracts the page may touch. Every transaction the page then requests "
        "is simulated and asserted against that envelope before anything is "
        "signed; anything over the autonomous ceiling waits for the owner. "
        "Off-chain signature requests (permits) are ALWAYS refused. After this, "
        "drive the page with the ordinary browser actions.",
        param_model=ConnectParams)
    async def dapp_connect(self, params: ConnectParams, execution_context=None):
        from core.wallet import chains
        from tools.dapp_browser import bridge as bridge_mod
        from tools.dapp_browser.js import new_binding_name, provider_script

        if not dapp_browser_enabled():
            return self._ar(error=(
                f"the dapp wallet is off — set {FLAG}=true to arm it. Nothing "
                f"was connected."))

        # 067 P1b: the kernel's leaf + principal, then the pause. CR-L21:
        # arming a spend envelope opens exposure — an entry.
        from core.money.authorize import SpendIntent, authorize_spend
        from tools.controller.turn_origin import (
            _is_forged_or_autonomous_turn as _owner_turn_probe)
        verdict = authorize_spend(
            SpendIntent(tool="dapp_browser", what="connect a wallet to a dapp",
                        entry=True), execution_context,
            forged_fn=_owner_turn_probe)
        if verdict.refused:
            suffix = " Nothing was connected." if verdict.step == "pause" else ""
            return self._ar(error=verdict.reason + suffix)

        ok, why = chains.money_capable(params.chain)
        if not ok:
            return self._ar(error=why)

        # H03b: the envelope is armed for ONE origin — the page's main frame
        # on the origin named here. Everything else is refused at the binding.
        armed_origin = bridge_mod.origin_of(params.url)
        if armed_origin is None:
            return self._ar(error=(
                f"{params.url!r} is not an http(s) page a wallet can be armed "
                f"for. Nothing was connected."))

        browser_context = getattr(execution_context, "browser_context", None)
        if browser_context is None:
            return self._ar(error=(
                "no browser session is available on this turn, so there is "
                "nothing to attach a wallet to."))

        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")

        allow = tuple(a.strip() for a in (params.allow_contracts or "").split(",")
                      if a.strip())
        try:
            from core.wallet.tokens import normalize_address
            allow = tuple(normalize_address(a) for a in allow)
        except Exception as exc:
            return self._ar(error=f"allow_contracts holds an invalid address ({exc})")

        key = self._key(execution_context)
        store = self._get_store()
        try:
            if store is None:
                raise OSError("dapp session store is unavailable")
            prior = store.get(key, user_id=self._uid(execution_context) or None)
            import math
            prior_spend = float((prior.envelope or {}).get("spent_usd", 0)) if prior else 0.0
            if not math.isfinite(prior_spend) or prior_spend < 0:
                raise ValueError("invalid stored spend")
        except Exception:
            return self._ar(error="Cannot verify the existing dapp session budget; nothing was connected")

        def _revoked():
            current = store.get(key, user_id=self._uid(execution_context) or None)
            return current is None or current.revoked

        envelope = bridge_mod.Envelope(
            chain=params.chain, max_spend_usd=params.max_spend_usd,
            session_budget_usd=params.session_budget_usd,
            allow_contracts=allow, spent_usd=prior_spend,
            approval_timeout_sec=params.approval_timeout_sec)
        wallet_bridge = bridge_mod.WalletBridge(
            envelope=envelope, wallet=wallet,
            execution_context=execution_context,
            container=getattr(self, "container", None),
            rail_factory=self._rail_factory, guard_fn=self._guard_fn,
            price_fn=self._price, rpc_fn=self._rpc_fn,
            approver=self._approver, persist_fn=self._persist_bridge,
            armed_origin=armed_origin,
            revocation_probe=_revoked,
            taint_probe=self._taint_probe_for(execution_context),
            turn_kind_probe=self._turn_kind_probe_for(execution_context))
        wallet_bridge.require_attached_page = True

        key = self._key(execution_context)
        previous = self._bridges.get(key)
        if previous is not None:
            # Revoke the old envelope BEFORE installing the new one: two live
            # envelopes on one page would mean two budgets, and the page would
            # spend whichever answered first.
            previous.envelope.revoked = True
            envelope.spent_usd = max(prior_spend, previous.envelope.spent_usd)
            envelope.sent = list(previous.envelope.sent)
        self._bridges[key] = wallet_bridge

        try:
            session = await browser_context.get_session()
            raw_context = session.context
            # A FRESH random binding per arming: a page cannot guess the entry
            # point, and a binding left from an earlier arming answers only for
            # its own (now revoked) envelope. Playwright cannot remove a
            # binding, which is why the name — not a flag — changes.
            binding = new_binding_name()
            await raw_context.expose_binding(binding, wallet_bridge.handle)
            await raw_context.add_init_script(provider_script(
                address=wallet_bridge.address,
                chain_id_hex=wallet_bridge.chain_id_hex(),
                binding=binding, origin=armed_origin))
        except Exception as exc:
            wallet_bridge.envelope.revoked = True
            self._bridges.pop(key, None)
            return self._ar(error=(
                f"could not install the wallet into the browser context: {exc}"))

        # The wallet is armed — record the envelope durably so a session that
        # connects and never spends is still visible after a restart (043 A37).
        if not wallet_bridge._persist():
            return self._ar(error="dapp budget could not be saved; wallet revoked before signing")

        # The script runs at DOCUMENT START, so a page already open does NOT
        # have it. Navigating now is what makes the dapp see a wallet — saying
        # so beats leaving the agent to wonder why Connect does nothing.
        try:
            await browser_context.navigate_to(params.url)
        except Exception as exc:
            return self._ar(error=(
                f"the wallet is installed but the page could not be opened: "
                f"{exc}. Navigate with browser_go_to_url and it will be there."))

        # Bind the envelope to THIS tab and revoke it the moment the tab leaves
        # the armed origin. A redirect that already left it (e.g. example.com ->
        # app.example.com) is refused now rather than armed for the wrong site.
        try:
            page = await browser_context.get_current_page()
            landed = bridge_mod.origin_of(page.main_frame.url)
            if landed != armed_origin:
                wallet_bridge.envelope.revoked = True
                self._persist_bridge(wallet_bridge)
                return self._ar(error=(
                    f"the page redirected from {armed_origin} to "
                    f"{landed or 'a non-web page'}, so the wallet was revoked. "
                    f"Connect again with url set to the page it lands on."))
            wallet_bridge.attach_page(page)
        except Exception as exc:
            wallet_bridge.envelope.revoked = True
            self._persist_bridge(wallet_bridge)
            return self._ar(error=(
                f"could not bind the wallet to the opened page ({exc}); it was "
                f"revoked. Nothing can be signed."))

        return self._ar(content=(
            f"wallet connected to {params.url}\n"
            f"  address:  {wallet_bridge.address}\n"
            f"  chain:    {params.chain} ({wallet_bridge.chain_id_hex()})\n"
            f"  per tx:   ${params.max_spend_usd:.2f}\n"
            f"  session:  ${params.session_budget_usd:.2f} total\n"
            f"  contracts: {', '.join(allow) if allow else 'any on this chain'}\n"
            f"  REFUSED ALWAYS: off-chain signatures (permits), contract "
            f"deployment from the page, and moving to another chain.\n"
            f"  Drive the page with the ordinary browser actions. A transaction "
            f"over the autonomous ceiling waits up to "
            f"{params.approval_timeout_sec:.0f}s for the owner, then the page "
            f"is told it was rejected."))

    @BaseTool.action(
        "What the dapp wallet has done this session: budget spent and left, "
        "every transaction signed, and every request REFUSED with the reason — "
        "which is usually why a page appears to be doing nothing.",
        param_model=NoParams)
    async def dapp_status(self, params: NoParams, execution_context=None):
        wallet_bridge = self._bridges.get(self._key(execution_context))
        if wallet_bridge is None:
            # No LIVE bridge on this turn. A restart empties `_bridges`, so read
            # the durable store: a saved record is reported (read-only — it
            # cannot spend), and only a genuine absence says "nothing connected".
            persisted = self._persisted_status(execution_context)
            if persisted is not None:
                return self._ar(content=persisted)
            return self._ar(content=(
                "no dapp wallet is connected on this session. Use dapp_connect "
                "to arm one."))
        env = wallet_bridge.envelope
        lines = [
            f"dapp wallet on {env.chain}"
            f"{' — REVOKED' if env.revoked else ''}",
            f"  address:  {wallet_bridge.address}",
            f"  spent:    ${env.spent_usd:.4f} of ${env.session_budget_usd:.2f} "
            f"(${env.remaining_usd():.4f} left)",
            f"  per tx:   ${env.max_spend_usd:.2f}",
            f"  signed:   {len(env.sent)}",
        ]
        for item in env.sent[-5:]:
            lines.append(f"    {item['tx']} -> {item['to']} "
                         f"({item['selector']}, ${item['usd']:.4f})")
        lines.append(f"  refused:  {len(env.refused)}")
        for item in env.refused[-5:]:
            lines.append(f"    {item.get('kind')}: {item.get('why')}")
        return self._ar(content="\n".join(lines) + "\n")

    @BaseTool.action(
        "Revoke the dapp wallet for this session. The page keeps its provider "
        "object but every request from it is refused from here on. Do this when "
        "you are finished with a dapp — an armed wallet on an open page is a "
        "standing authorization.",
        param_model=NoParams)
    async def dapp_disconnect(self, params: NoParams, execution_context=None):
        wallet_bridge = self._bridges.get(self._key(execution_context))
        if wallet_bridge is None:
            return self._ar(content="no dapp wallet was connected on this session.")
        wallet_bridge.envelope.revoked = True
        self._persist_bridge(wallet_bridge)
        env = wallet_bridge.envelope
        return self._ar(content=(
            f"dapp wallet REVOKED. It signed {len(env.sent)} transaction(s) for "
            f"${env.spent_usd:.4f}. Further requests from the page are refused."))
