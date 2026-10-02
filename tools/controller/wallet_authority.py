"""Wallet authorization runs before approval exemptions and tool execution.

067 P1b: the principal half is the money kernel's ``authorize_spend`` with the
session binding (the session's user must be the owner, a context is required,
and the context's user must be the session's). No leaf, turn or pause bar
here: those belong to the verb and its guard, which also admit the autonomous
lane this hook must not refuse.
"""
from core.money.authorize import SpendIntent, authorize_spend
from core.money.classify import money_action, money_tool

_PRINCIPAL_ONLY = SpendIntent(leaf=False, turn=False, pause=False)


def make_wallet_authority_hook(controller):
    def check(name, params, context):
        details = controller.get_action_details(name)
        tool_id = getattr(details, 'tool', None)
        if not (money_action(name) or (tool_id and money_tool(tool_id))):
            return None
        verdict = authorize_spend(_PRINCIPAL_ONLY, context,
                                  session_user=controller.user_id)
        return verdict.reason if verdict.refused else None
    return check
