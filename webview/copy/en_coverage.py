"""Words of the console's coverage wave (113 W6): the empty list setting, the
chat column, AI cost on Money › Cash, the model picker on Agent › Settings and
offers and orders on Money.

Part of the console copy table (:mod:`webview.copy.en` merges it into
``STRINGS``); split out so the one table stays under the file-size ceiling.
"""

COVERAGE = {
    # Agent › Settings — a list setting with no entries (113 W6 item 1).
    "agent.set_list_none": "No entries",
    # The start page holds a picked file until the first line makes the chat
    # (113 W6 item 2). Read by static/app/file-attach.js off #attach-copy.
    "chat.attach_held": "{name} goes with your first line.",
    # Money › Cash › AI cost — the /usage view (113 W6 item 4). Read by
    # static/app/money-usage.js off #money-usage-copy; the lines are
    # core.status_economics.spend_view_lines, the same lines every seat prints.
    "money.usage.title": "Where the AI cost went",
    "money.usage.period": "Period",
    "money.usage.days_1": "24 hours",
    "money.usage.days_7": "7 days",
    "money.usage.days_30": "30 days",
    "money.usage.loading": "Reading what the AI use cost…",
    "money.usage.unreadable": "I could not read what the AI use cost. This is not a zero.",
    "money.usage.why": "Why",
    # Agent › Settings › the model a new chat starts on (113 W6 item 5): the
    # /model <name> default pipeline. Read by static/app/agent-model.js off
    # #agent-model-copy; the answer after a change is the pipeline's own text.
    "agent.model.title": "Model for new chats",
    "agent.model.aside": "A chat already open keeps its model",
    "agent.model.now": "New chats start on {model} ({provider}).",
    "agent.model.source": "Chosen by: {source}",
    "agent.model.provider_default": "the provider's default model",
    "agent.model.label": "Model name or alias",
    "agent.model.save": "Use for new chats",
    "agent.model.reset": "Use the operator's default",
    "agent.model.loading": "Reading the model for new chats…",
    "agent.model.unreadable": "I could not read your model choice.",
    "agent.model.why": "Why",
    # Money › Invoices › what the agent sells and its orders (113 W6 item 6). Read by
    # static/app/money-offers.js off #money-offers-copy; an answer to Pause or
    # Resume is the /offer verb's own text.
    "money.offers.title": "What the agent sells",
    "money.offers.loading": "Reading the offers…",
    "money.offers.unreadable": "I could not read the offers. This is not the same as no offers.",
    "money.offers.why": "Why",
    "money.offers.empty": "The agent sells nothing yet.",
    "money.offers.empty_how": "Say \"sell …\" in chat and the agent drafts an offer for you to confirm.",
    "money.offers.sold": "{count} sold",
    "money.offers.pause": "Pause",
    "money.offers.resume": "Resume",
    "money.offers.unreachable": "The change did not reach the agent. Try again in a moment.",
    "money.offers.decide_note": "To set up, go live, settle an order or retire an offer, use /offer or the decision the agent sends you.",
    "money.offers.orders_title": "Orders",
    "money.offers.orders_empty": "No orders yet.",
    "money.offers.orders_more": "These are the newest orders. /offer orders lists them by offer.",
    "money.offers.state_draft": "Draft",
    "money.offers.state_setup": "Being set up",
    "money.offers.state_live": "On sale",
    "money.offers.state_paused": "Paused",
    "money.offers.state_retired": "Retired",
    "money.offers.order_quoted": "Price sent",
    "money.offers.order_awaiting_payment": "Waiting for payment",
    "money.offers.order_paid": "Paid",
    "money.offers.order_fulfilling": "Being made",
    "money.offers.order_delivered": "Delivered",
    "money.offers.order_accepted": "Accepted",
    "money.offers.order_disputed": "Disputed",
    "money.offers.order_failed": "Did not finish",
    "money.offers.order_expired": "Expired",
    "money.offers.order_cancelled": "Cancelled",
    # The Work pane's last browser screenshot (113 W6 item 7). Read by
    # static/app/workpane-shot.js off #workpane-shot-copy.
    "workpane.shot_title": "What the browser showed last",
    "workpane.shot_loading": "Looking for a browser screenshot…",
    "workpane.shot_none": "The browser took no screenshot in this chat.",
    "workpane.shot_unreadable": "I could not read this chat's screenshots.",
    "workpane.shot_alt": "The last screenshot the browser took in this chat",
}
