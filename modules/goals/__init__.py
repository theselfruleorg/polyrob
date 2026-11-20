"""The durable goal board (proposal 125 S29): the store, its asks and dependencies,
its state machines and the small helpers the board needs (payload keys, the
in-process run cancel, the rail answers, the claim liveness check).

It sits in the modules tier so a module (the x402 settlement notice, for one)
reaches the board without an upward import. The dispatcher, the goal runs and
the rest of the goal loop stay in ``agents/task/goals/``.
"""
