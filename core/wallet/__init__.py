"""Agent personal wallet (core-tier). The agent's own wallet, used as a tool.

Deliberately separate from the server-tier tariffing gateway (modules/x402,
modules/payments). MUST NOT import any server-tier billing module.
"""


def _register_money_hooks() -> None:
    """067 P5a: the wallet supplies every rail fact the money kernel reads.

    ONE place, run when this package is imported (the pack loader's phase 1
    imports it at every process entry; the signer imports it through
    ``core.wallet.tx_guard``). Stdlib-only here: each provider imports its
    module on first USE, late-bound so a monkeypatched function is what the
    kernel calls. P5b moves this function into the wallet pack's registration.
    """
    from core.money import hooks

    def _cap_resolver(*args, **kwargs):
        from core.wallet.config import live_caps_resolver
        return live_caps_resolver(*args, **kwargs)

    def _spend_ledger():
        from core.wallet.factory import get_policy_gate
        return get_policy_gate()

    async def _treasury_balance():
        from core.wallet.treasury_balance import treasury_balance_usd
        return await treasury_balance_usd()

    def _submission_journal():
        from core.wallet import submission_journal
        return submission_journal

    def _position_book(user_id, deltas):
        from core.open_positions import apply_deltas
        return apply_deltas(user_id, deltas)

    hooks.register_cap_resolver(_cap_resolver)
    hooks.register_spend_ledger(_spend_ledger)
    hooks.register_treasury_balance(_treasury_balance)
    hooks.register_submission_journal(_submission_journal)
    hooks.register_position_book(_position_book)


_register_money_hooks()
