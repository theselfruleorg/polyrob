"""The MISSING half of the action-name ratchet.

`tests/unit/core/test_action_name_parity.py` asserts every name IN a policy set is
a real runtime action (catches a DEAD entry). Nothing asserted the reverse: that
every money-SPEND action is IN the lanes that are supposed to cover it. That is
exactly how `x402_pay_x402_fetch` shipped with no approval lane and no
forged-turn refusal while the whole suite passed (audit 2026-08-22, H1a).

This file is the other direction. When a NEW money verb is added, it fails until
the verb joins every lane — or is added to the reviewed exemption list below with
a written reason.
"""
import inspect

import pytest

from tests.unit.core.test_action_name_parity import (  # reuse the derivation
    _register_every_optional_tool, _container_tool_action_names,
    _direct_action_names, _namespaced,
)


@pytest.fixture(scope="module")
def runtime_names() -> set:
    from tools.descriptors import TOOL_COMPONENTS, TOOL_DESCRIPTORS
    before_keys = set(TOOL_DESCRIPTORS)
    before_classes = {k: getattr(v, "tool_class", None)
                      for k, v in TOOL_DESCRIPTORS.items()}
    before_components = list(TOOL_COMPONENTS)
    try:
        _register_every_optional_tool()
        yield _container_tool_action_names() | _direct_action_names()
    finally:
        for key in set(TOOL_DESCRIPTORS) - before_keys:
            TOOL_DESCRIPTORS.pop(key, None)
        for key, cls in before_classes.items():
            if key in TOOL_DESCRIPTORS:
                TOOL_DESCRIPTORS[key].tool_class = cls
        TOOL_COMPONENTS[:] = before_components


#: Verbs owned by a `money`-capability tool that do NOT move value OUT and so are
#: deliberately off the spend lanes. Every entry needs a one-line reason. SHRINK
#: this set; never grow it to silence a failure.
NON_SPEND_MONEY_VERBS = {
    # -- x402_pay — read-only pricing/discovery, never signs. -----------------
    "x402_pay_x402_quote": "prices a resource; never pays",
    "x402_pay_x402_probe": "read-only paywall probe; never pays",
    "x402_pay_x402_sweep": "read-only paywall sweep; never pays",
    "x402_pay_x402_wallet_status": "read-only wallet/caps view",
    # -- x402_invoice — RECEIVE side. x402_request is on the lane via the
    # receive carve-out; the rest are reads. ----------------------------------
    "x402_invoice_x402_invoices": "lists own invoices; receives nothing",
    "x402_invoice_accounting": "read-only ledger view",

    # -- hyperliquid (real gated trading tool) — reads/risk-management --------
    "hyperliquid_get_perpetual_markets": "read-only market/account data; no fund movement",
    "hyperliquid_get_spot_markets": "read-only market/account data; no fund movement",
    "hyperliquid_get_current_price": "read-only market/account data; no fund movement",
    "hyperliquid_get_all_mids": "read-only market/account data; no fund movement",
    "hyperliquid_get_orderbook": "read-only market/account data; no fund movement",
    "hyperliquid_get_funding_rate": "read-only market/account data; no fund movement",
    "hyperliquid_get_account_state": "read-only market/account data; no fund movement",
    "hyperliquid_get_spot_balances": "read-only market/account data; no fund movement",
    "hyperliquid_get_open_orders": "read-only market/account data; no fund movement",
    "hyperliquid_get_fills": "read-only market/account data; no fund movement",
    "hyperliquid_agent_status": "read-only market/account data; no fund movement",
    "hyperliquid_cancel_order": "cancels a pending order; does not move funds",
    "hyperliquid_cancel_all_orders": "cancels a pending order; does not move funds",
    "hyperliquid_update_leverage": "changes margin/leverage ratio; does not move funds directly",
    "hyperliquid_approve_agent": (
        "authorizes a trading-only delegate key (Hyperliquid agent wallets "
        "cannot withdraw); no fund movement"
    ),
    "hyperliquid_revoke_agent": (
        "revokes a trading-only delegate key; no fund movement"
    ),
    # hyperliquid_place_limit_order / hyperliquid_place_market_order are the
    # real SPEND verbs and stay ON the lane (not exempted here).

    # -- polymarket (real gated trading tool) — reads/risk-management ---------
    "polymarket_search_markets": "read-only market data; no fund movement",
    "polymarket_get_trending_markets": "read-only market data; no fund movement",
    "polymarket_filter_markets_by_category": "read-only market data; no fund movement",
    "polymarket_get_featured_markets": "read-only market data; no fund movement",
    "polymarket_get_closing_soon_markets": "read-only market data; no fund movement",
    "polymarket_get_sports_markets": "read-only market data; no fund movement",
    "polymarket_get_crypto_markets": "read-only market data; no fund movement",
    "polymarket_get_market_details": "read-only market data; no fund movement",
    "polymarket_get_current_price": "read-only market data; no fund movement",
    "polymarket_get_orderbook": "read-only market data; no fund movement",
    "polymarket_get_spread": "read-only market data; no fund movement",
    "polymarket_get_market_volume": "read-only market data; no fund movement",
    "polymarket_get_all_positions": "read-only market data; no fund movement",
    "polymarket_get_portfolio_summary": "read-only market data; no fund movement",
    "polymarket_get_trade_history": "read-only market data; no fund movement",
    # CLOB-authenticated reads; still no value movement (see the "stay on the
    # gated trade tool (CLOB auth)" comment at tools/polymarket/service.py).
    "polymarket_get_balance": "read-only balance/order-status (CLOB-authenticated); no fund movement",
    "polymarket_get_open_orders": "read-only balance/order-status (CLOB-authenticated); no fund movement",
    "polymarket_get_order_history": "read-only balance/order-status (CLOB-authenticated); no fund movement",
    "polymarket_cancel_order": "cancels a pending order; does not move funds",
    "polymarket_cancel_all_orders": "cancels a pending order; does not move funds",
    # polymarket_place_limit_order / polymarket_place_market_order are the real
    # SPEND verbs and stay ON the lane (not exempted here).

    # -- launchpad (042) — the two READ verbs. Both are eth_call only: they
    # build no transaction, hold no signer and reach no rail. The three write
    # verbs (launch/buy/sell) stay ON the lane. ------------------------------
    "launchpad_quote": "prices a curve trade with eth_call; signs nothing",
    "launchpad_status": "reads a token's curve state; signs nothing",

    # -- dapp_browser (042) — the two non-authorizing verbs. `dapp_connect` is
    # the money verb (it grants the envelope) and stays ON the lane; these two
    # only report it and take it away. ---------------------------------------
    "dapp_browser_dapp_status": "reports the session envelope; authorizes nothing",
    "dapp_browser_dapp_disconnect": "REVOKES the envelope; can only reduce authority",

    # NOTE: `hyperliquid_data` / `polymarket_data` (HyperliquidDataTool /
    # PolymarketDataTool) carry NO entries here on purpose. They are separate,
    # non-money tool_ids (`TOOL_CAPABILITIES["hyperliquid_data"] == frozenset()`,
    # same for polymarket_data) — `_money_verbs` below resolves EXACT tool
    # ownership per name, so their verbs never enter the money-verb set in the
    # first place and need no exemption. (They used to leak in under an earlier,
    # prefix-based derivation — fixed below; see `_container_tool_action_owners`.)
}


def _money_tool_ids() -> set:
    from core.tool_capabilities import ids_with
    return set(ids_with("money"))


def _container_tool_action_owners() -> dict:
    """Runtime action name -> its EXACT owning tool_id (the capability id gates and
    `TOOL_CAPABILITIES` key on), built from the SAME reflection walk
    ``test_action_name_parity._container_tool_action_names`` performs — but keeping
    the per-descriptor owner instead of flattening straight to a bare name set.

    Fixes a real bug in an earlier version of this file: matching money verbs by
    NAME PREFIX ("does this name start with 'hyperliquid_'?") also matched the
    SEPARATE, non-money ``hyperliquid_data``/``polymarket_data`` tool_ids, because
    their own namespaced names happen to start with the same string
    (``hyperliquid_data_get_account_state`` starts with ``"hyperliquid_"``). Those
    tool_ids carry an explicit empty capability set in ``TOOL_CAPABILITIES``
    (core/tool_capabilities.py) — they are not money tools at all — but a prefix
    test cannot distinguish "the hyperliquid tool" from "a tool_id that happens to
    start with hyperliquid". Exact ownership can, because it resolves the SAME
    ``tool_id`` value the parity-test walk computes per descriptor
    (``CATALOG_ALIASES.get(descriptor_id, descriptor_id)``) rather than
    pattern-matching the rendered name after the fact.

    Both namespaced forms a descriptor can render (the capability id form and the
    descriptor id form — see ``test_action_name_parity._container_tool_action_names``'s
    own comment on ``browser_manager`` -> ``browser``) map to the SAME owner: the
    capability id, since that is what ``TOOL_CAPABILITIES``/``ids_with`` key on.

    Directly-registered actions (``_direct_action_names``) have NO owning tool_id
    by construction — they are registered straight on the registry via
    ``@registry.action``, not through a container tool class, so they never appear
    in this map and can never be classified as money-tool verbs. That is correct
    for this codebase: every ``money``-capability tool (defi_trade, hyperliquid,
    polymarket, x402_invoice, x402_pay) is a container tool whose actions ARE
    covered by this walk, so nothing money-relevant is silently dropped by
    excluding direct actions.
    """
    from tools.descriptors import TOOL_DESCRIPTORS
    from core.tool_capabilities import CATALOG_ALIASES

    owners: dict = {}
    for descriptor_id, desc in TOOL_DESCRIPTORS.items():
        cls = getattr(desc, "tool_class", None)
        if cls is None:
            continue
        tool_id = CATALOG_ALIASES.get(descriptor_id, descriptor_id)
        for attr in dir(cls):
            if attr.startswith("_"):
                continue
            try:
                member = inspect.getattr_static(cls, attr)
            except Exception:
                continue
            if not callable(member):
                continue
            if not (hasattr(member, "action_info") or hasattr(member, "_description")):
                continue
            owners[_namespaced(tool_id, attr)] = tool_id
            owners[_namespaced(descriptor_id, attr)] = tool_id
    return owners


def _money_verbs(runtime_names: set) -> set:
    """Every runtime action name whose EXACT owning tool_id carries `money`.

    Resolves ownership via ``_container_tool_action_owners()`` (exact, per-descriptor)
    rather than a name-prefix test — see that helper's docstring for the bug this
    replaces. ``runtime_names`` is used as a sanity bound: only names the already-
    validated derivation actually produced are considered.
    """
    ids = _money_tool_ids()
    owners = _container_tool_action_owners()
    return {n for n, owner in owners.items() if owner in ids and n in runtime_names}


def test_the_derivation_is_not_vacuous(runtime_names):
    verbs = _money_verbs(runtime_names)
    assert len(verbs) >= 6, f"only derived {len(verbs)} money verbs — derivation broke"
    assert "x402_pay_x402_fetch" in verbs


def test_every_money_spend_verb_is_on_an_owner_approval_lane(runtime_names):
    """A verb that moves value out must reach the owner, or be explicitly
    exempted here with a reason.

    Two lanes are legal (039). Most verbs ride the shared pre-hook
    (`PAYMENT_APPROVAL_TOOLS`). A verb may instead own its gate inside itself —
    but only by declaring it in `VERB_OWNED_APPROVAL_GATES`, so the exception is a
    registry entry someone had to write rather than a verb quietly absent from
    both lists. Being in BOTH is also a failure: that is two owner taps for one
    action, which is what the owner hit on 2026-09-12.
    """
    from core.config_policy import (PAYMENT_APPROVAL_TOOLS,
                                    VERB_OWNED_APPROVAL_GATES)

    spend = _money_verbs(runtime_names) - set(NON_SPEND_MONEY_VERBS)
    shared, owned = set(PAYMENT_APPROVAL_TOOLS), set(VERB_OWNED_APPROVAL_GATES)

    missing = sorted(spend - shared - owned)
    assert not missing, (
        f"money-spend verbs {missing} are on NO owner-approval lane — they can "
        f"move value with nobody asked. Add them to PAYMENT_APPROVAL_TOOLS or to "
        f"VERB_OWNED_APPROVAL_GATES in core/config_policy/payment_tools.py, or to "
        f"NON_SPEND_MONEY_VERBS in this file with a written reason."
    )

    doubled = sorted(spend & shared & owned)
    assert not doubled, (
        f"money-spend verbs {doubled} are on BOTH lanes — the owner is asked "
        f"twice for one action, from two prompts describing it differently."
    )


def test_every_money_spend_verb_is_high_impact_by_NAME(runtime_names):
    """A correspondent-tainted session must be blocked by the NAME layer alone,
    not only by tool-id resolution (which a resolver fault can void)."""
    from agents.task.agent.core.correspondent_gate import is_high_impact

    spend = _money_verbs(runtime_names) - set(NON_SPEND_MONEY_VERBS)
    unprotected = sorted(v for v in spend if not is_high_impact(v))
    assert not unprotected, (
        f"{unprotected} are not blocked by the correspondent-gate NAME layer")


def test_every_money_tool_is_delegate_blocked():
    """A leaf sub-agent must never inherit a money tool."""
    from core.tool_capabilities import ids_with

    money = set(ids_with("money"))
    blocked = set(ids_with("delegate_blocked"))
    leaked = sorted(money - blocked)
    assert not leaked, (
        f"money tools {leaked} are not delegate_blocked — a leaf sub-agent would "
        f"inherit them")


def test_venue_order_verbs_are_on_the_lane(runtime_names):
    """Hyperliquid/Polymarket ARE `money`-capability tools (core/tool_capabilities.py
    tags both `money`) and their place-order verbs ARE covered by the general
    spend-lane test above via `_money_verbs`. This test pins the two place-order
    verbs per venue by exact literal name as a defense-in-depth check independent
    of that general derivation — venues are also tagged `readable_while_tainted`
    (not `high_impact`) so their read verbs stay available to a correspondent-
    tainted session; only the place-order verbs are meant to sit on the payment-
    approval lane. A rename or an action-set change to either venue tool then
    fails here by exact verb name, not just via the broader derivation."""
    from core.config_policy import PAYMENT_APPROVAL_TOOLS

    for verb in ("hyperliquid_place_limit_order", "hyperliquid_place_market_order",
                 "polymarket_place_limit_order", "polymarket_place_market_order"):
        assert verb in runtime_names, f"{verb} is not a runtime name — check went vacuous"
        assert verb in PAYMENT_APPROVAL_TOOLS, f"{verb} left the approval lane"
