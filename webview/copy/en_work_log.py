"""Work › Log words: one owner sentence per event kind the owner would otherwise
read as a machine name (``console_goal_verb``, ``cron_cancelled``,
``owner_command``, ``goal_status_paused``…). Part of the console copy table
(:mod:`webview.copy.en` merges it into ``STRINGS``).

``webview/worklog_api.py::owner_words`` picks the key by kind
(``work.log.kind.<kind>``) and fills the named fields from the event's own
record; a kind with no key keeps the summary the activity stream wrote. A goal's
new status reads ``work.log.goal_state.<status>``; a seat reads
``work.log.seat.<seat>``.
"""

WORK_LOG = {
    # The owner's own acts get their own filter chip (core.activity_class "owner").
    "work.log.class_owner": "What you did",

    # The reason the agent recorded for an act (proposal 122 W1, the act record's `why`).
    "work.log.reason": "Why: {why}",

    # Fallback nouns when the record names no title.
    "work.log.a_goal": "a goal",
    "work.log.a_job": "a scheduled job",

    # The owner's console acts (043 W4: one kind per console write).
    "work.log.kind.console_goal_verb": "You pressed {button} on the goal {title}.",
    "work.log.kind.console_goal_create": "You added the goal {title}.",
    "work.log.kind.console_cron_cancel": "You stopped the scheduled job {job}.",
    "work.log.kind.console_cron_create": "You added the scheduled job {job}.",
    "work.log.kind.console_cron_verb": "You pressed {button} on the scheduled job {job}.",
    "work.log.kind.console_config_write": "You changed the setting {setting}.",
    "work.log.kind.console_pref_write": "You changed the setting {setting}.",
    "work.log.kind.console_pending_decide": "You answered a request that waited for you.",
    "work.log.kind.console_inbox_decide": "You answered a request in the Inbox.",
    "work.log.kind.console_invoice_settle": "You checked an invoice for its payment.",
    "work.log.kind.console_token_decide": "You changed how the agent treats a token.",
    "work.log.kind.console_card_press": "You answered a decision.",
    "work.log.kind.console_chat_verb": "You renamed or closed a chat.",
    "work.log.kind.console_model_set": "You chose the model for new chats.",
    "work.log.kind.console_offer_verb": "You paused or resumed an offer.",
    "work.log.kind.console_self_context_write": "You changed what the agent knows about itself.",
    "work.log.kind.console_memory_write": "You changed the agent's memory.",

    # An owner command typed on a seat (116 W3.7). The verb is the command itself.
    "work.log.kind.owner_command": "You sent the command {verb} from {seat}.",
    "work.log.seat.telegram": "Telegram",
    "work.log.seat.repl": "the terminal",
    "work.log.seat.terminal": "the terminal",
    "work.log.seat.console": "this console",
    "work.log.seat.webview": "this console",
    "work.log.seat.slack": "Slack",
    "work.log.seat.discord": "Discord",
    "work.log.seat.signal": "Signal",
    "work.log.seat.whatsapp": "WhatsApp",
    "work.log.seat.email": "email",

    # A job's own events (043 A29), whichever seat drove them. {who} is
    # work.log.who_you or work.log.who_agent.
    "work.log.who_you": "You",
    "work.log.who_agent": "The agent",
    "work.log.kind.cron_scheduled": "{who} added the scheduled job {job}.",
    "work.log.kind.cron_cancelled": "{who} stopped the scheduled job {job}.",
    "work.log.kind.cron_edited": "{who} changed the scheduled job {job}.",
    "work.log.kind.cron_paused": "{who} paused the scheduled job {job}.",
    "work.log.kind.cron_resumed": "{who} resumed the scheduled job {job}.",
    "work.log.kind.cron_run_now": "{who} asked the scheduled job {job} to run now.",

    # A goal's new status (the goal board's `status_<to>` event).
    "work.log.kind.goal_status": "The goal {title} {state}.",
    "work.log.goal_state.ready": "is ready to run",
    "work.log.goal_state.running": "is running",
    "work.log.goal_state.paused": "is paused",
    "work.log.goal_state.blocked": "needs a fix",
    "work.log.goal_state.waiting": "waits for other goals",
    "work.log.goal_state.triage": "is being sorted",
    "work.log.goal_state.done": "is done",
    "work.log.goal_state.cancelled": "is stopped",
    "work.log.goal_state.other": "changed its status",
}

# Work › Scheduled jobs: Stop asks first, and every answer names the job, never its id.
WORK_LOG.update({
    "work.schedule.cancel_ask": "Stop {job} for good? It will not run again.",
    "work.schedule.cancel_yes": "Yes, stop it",
    "work.schedule.cancel_no": "Keep it",
    "work.schedule.cancelled": "Stopped {job}. It will not run again.",
    "work.schedule.cancel_failed": "I could not stop {job}. It may have stopped already.",
    "work.schedule.no_job": "There is no such scheduled job.",
    # The scheduler-off remedy in console words: the same change as the chat
    # line cron.service.SCHEDULER_ON_CHAT_LINE, named as the Agent › Settings answer.
    "work.schedule.off_note": (
        "Scheduled jobs are turned off, so nothing will run this job. To turn them on, "
        "open Agent › Settings and let the agent start work on its own, all of it. "
        "The change applies after a restart."
    ),
})

# One remedy for "autonomy off" on every Work tab (the chat seats say the same
# change as cron.service.SCHEDULER_ON_CHAT_LINE): no shell command, no flag name.
WORK_LOG.update({
    "work.now.off": (
        "The agent's own work is off. Nothing starts until you ask, and a stored goal "
        "does not run. To turn it on, open Agent › Settings and let the agent start "
        "work on its own, all of it. The change applies after a restart."
    ),
    "work.schedule.off": (
        "The scheduler is off, so nothing will run. These jobs are stored and run again "
        "when it is on. To turn it on, open Agent › Settings and let the agent start "
        "work on its own, all of it. The change applies after a restart."
    ),
    "work.schedule.a_job": "this job",
})
