"""Offer-agnostic commerce (099): offers, resources, orders, listings.

Core knows the SHAPE of an offer, never what it sells. Every module here is
tier 0 (imports nothing above ``core``); an upward action — enqueue the
fulfillment goal, publish a listing — goes through a hook in
:mod:`core.commerce.hooks` that ``polyrob.app`` fills.

* :mod:`core.commerce.offer`     — the manifest (validation, pure)
* :mod:`core.commerce.store`     — ``commerce.db``: offers + resources
* :mod:`core.commerce.orders`    — the order state machine
* :mod:`core.commerce.hooks`     — the fulfiller + listing registries
* :mod:`core.commerce.listing`   — the listing projections (JSON-LD, A2A, MCP)

The order SERVICE (invoice an order, settle it, deliver it) and the buyer seat
live one tier up, in ``modules/commerce/``, because they need the invoice store.
"""
