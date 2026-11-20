"""The owner money verbs' decision bodies (proposal 125 §3.2, D1).

A seat (Telegram, the REPL, ``polyrob wallet``) parses the words and shows the
reply; the decision — which tool call, with which bound, and what the result
means — lives here, one tier above the rails it calls, so every seat runs the
same body. Each tool call runs through ``tools.controller.policy_chain.run_seat_action``:
the operator denylist (``POLYROB_TOOL_DENYLIST`` and the owner's ``approvals.deny``)
and the effect gate bind an owner verb exactly as they bind an agent call.

A seat imports the verb module (``tools.defi.owner_verbs.send``); it never imports
another seat's ``*_ops`` module (``tests/test_seat_procedure_ratchet.py``).
"""
