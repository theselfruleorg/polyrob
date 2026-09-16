"""Wallet authorization runs before approval exemptions and tool execution."""
from core.wallet.authority import money_action, money_tool, owner_refusal, turn_refusal


def make_wallet_authority_hook(controller):
    def check(name, params, context):
        details = controller.get_action_details(name)
        tool_id = getattr(details, 'tool', None)
        if not (money_action(name) or (tool_id and money_tool(tool_id))):
            return None
        error = owner_refusal(controller.user_id)
        if context is None:
            error = error or 'money actions require an authenticated execution context'
        else:
            error = error or turn_refusal(context)
            if getattr(context, 'user_id', None) != controller.user_id:
                error = 'money action identity does not match its session'
        if error:
            return error
        return None
    return check
