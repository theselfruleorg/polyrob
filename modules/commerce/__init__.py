"""The commerce service tier (099): orders on top of the ONE invoice store.

* :mod:`modules.commerce.service`    — create / pay / fulfill / deliver an order
* :mod:`modules.commerce.fulfillment`— the fulfillment goal's task and payload
* :mod:`modules.commerce.storefront` — the buyer seat (deterministic, no model)

The data and the state machines are in ``core.commerce``.
"""
