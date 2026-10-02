"""The money kernel (067 P1b) — mechanism, no rail data.

ONE owner per money concern, below every pack:

* :mod:`core.money.authority` — principal / turn / leaf / pause refusals;
* :mod:`core.money.classify`  — "is this a money tool / money action";
* :mod:`core.money.ledger`    — ``SpendLedger``: caps, rolling window, replay;
* :mod:`core.money.authorize` — ``authorize_spend``: the refusals composed in
  ONE fixed order;
* :mod:`core.money.regime`    — the supervised/autonomous/armed shape;
* :mod:`core.money.hooks`     — what a rail registers (cap values, the process
  ledger, the treasury balance).

Rail specifics (simulation, asserted deltas, venue flags, arrival proof) stay
with the rail. This package imports nothing above ``core``
(``tests/test_layering_ratchet.py``); the submodules are imported lazily so a
plain ``import core.money`` costs nothing.
"""
