"""Agent › Settings words: one owner label and one help line per setting (113 W5, CP-1).

``core/prefs.py`` keeps a DEVELOPER note on each ``PrefSpec`` (``description``:
merge rules, proposal ids, call sites). The console showed that note as the
label. The owner words live here instead: ``pref.<key>.label`` and
``pref.<key>.help`` for every key of ``core.prefs.PREF_SCHEMA``
(``tests/unit/webview/test_settings_copy.py`` fails on a key with no words).
The developer note stays where developers read it: ``polyrob doctor --flags``.

It also holds the words of two Agent detail views (a skill's text on Abilities,
"What the agent did" on Memory). Part of the console copy table
(:mod:`webview.copy.en` merges it into ``STRINGS``).
"""

PREF_WORDS = {
    # The group headings: when a change takes effect (PrefSpec.applies).
    "agent.set_group_now": "Takes effect now",
    "agent.set_group_next_chat": "In the next chat",
    "agent.set_group_restart": "After a restart",
    "agent.set_nothing_changed": "Nothing changed.",

    # 113 W5 (C-14): a skill's own text on Abilities, and what the agent did on Memory.
    "agent.cap_skill_read": "Read the skill",
    "agent.cap_skill_gone": "This skill is not there any more.",
    "agent.cap_skill_unreadable": "I could not read this skill.",
    "agent.mem_ep_title": "What the agent did",
    "agent.mem_ep_aside": "the last {count} runs",
    "agent.mem_ep_empty": "The agent has not recorded a run yet.",
    "agent.mem_ep_unreadable": "I could not read what the agent did.",
    "agent.mem_ep_col_what": "What",
    "agent.mem_ep_col_outcome": "How it ended",
    "agent.mem_ep_col_day": "Day",

    "pref.ui.theme.label": "Appearance",
    "pref.ui.theme.help": "Follow the system, or always dark or light.",
    "pref.ui.show_avatar.label": "Show the agent's picture",
    "pref.ui.show_avatar.help": "Show the agent's avatar in this console.",

    "pref.approvals.require.label": "Actions that need your approval",
    "pref.approvals.require.help": "One action name per line. The agent asks you before each one.",
    "pref.approvals.provider.label": "How approvals reach you",
    "pref.approvals.provider.help": "Where the agent waits for your yes or no. The stricter of this and the server setting wins.",
    "pref.approvals.deny.label": "Actions the agent may never run",
    "pref.approvals.deny.help": "One action name per line. To take a name off the list, you approve the change in the Inbox.",

    "pref.shell.deny.label": "Commands the agent may never run",
    "pref.shell.deny.help": "One command pattern per line, for example rm -rf *. Refused before any approval.",
    "pref.shell.allow.label": "Commands that run without asking",
    "pref.shell.allow.help": "One command pattern per line. The safety checks still apply.",

    "pref.budget.wallet_daily_usd.label": "Most the wallet may spend in a day (USD)",
    "pref.budget.wallet_daily_usd.help": "The lower of this and the server limit applies.",
    "pref.budget.wallet_per_tx_usd.label": "Most one payment may spend (USD)",
    "pref.budget.wallet_per_tx_usd.help": "Never more than the daily limit.",
    "pref.budget.defi_autonomous_usd.label": "Spend that runs without asking you (USD)",
    "pref.budget.defi_autonomous_usd.help": "Above this amount the agent asks first. Never more than one payment may spend.",

    "pref.goals.daily_quota.label": "Goal runs per day",
    "pref.goals.daily_quota.help": "How many times a day the agent may start work on its goals.",
    "pref.goals.max_concurrent.label": "Goals at the same time",
    "pref.goals.max_concurrent.help": "How many goals may run at once.",
    "pref.goals.notify_on_done.label": "Tell me when a goal is done",
    "pref.goals.notify_on_done.help": "Send a message each time a goal finishes.",

    "pref.voice.replies.label": "Answer with a voice note too",
    "pref.voice.replies.help": "On Telegram. Each voice note costs a speech call.",
    "pref.voice.transcript_echo.label": "Show what I heard",
    "pref.voice.transcript_echo.help": "Before it answers a voice note, the agent writes back what it heard.",
    "pref.stream.telegram.label": "Write the answer as it comes",
    "pref.stream.telegram.help": "On Telegram, one message grows while the agent writes it.",
    "pref.progress.telegram.label": "Show progress while it works",
    "pref.progress.telegram.help": "On Telegram, one message shows the step the agent is on.",

    "pref.delivery.lifecycle_daily_cap.label": "Status notes per day",
    "pref.delivery.lifecycle_daily_cap.help": "Short notes such as \"I learned something\". 0 turns the separate count off.",
    "pref.delivery.rate_per_hour.label": "Messages from the agent per hour",
    "pref.delivery.rate_per_hour.help": "Messages it starts by itself. A server limit, when set, is the most.",
    "pref.delivery.daily_cap.label": "Messages from the agent per day",
    "pref.delivery.daily_cap.help": "Messages it starts by itself. A server limit, when set, is the most.",

    "pref.digest.enabled.label": "Daily summary",
    "pref.digest.enabled.help": "One message a day with what the agent did.",
    "pref.digest.channel.label": "Where the daily summary goes",
    "pref.digest.channel.help": "The chat app that gets the daily summary.",
    "pref.digest.quiet_hours.label": "Quiet hours",
    "pref.digest.quiet_hours.help": "Hours with no messages from the agent, as start-end, for example 23-08. Messages wait until the end.",
    "pref.digest.timezone.label": "Your time zone",
    "pref.digest.timezone.help": "For example Europe/Berlin. Quiet hours use it. Empty uses the server clock.",

    "pref.style.verbosity.label": "How much the agent writes",
    "pref.style.verbosity.help": "Short, normal, or with full detail.",
    "pref.style.language.label": "Answer language",
    "pref.style.language.help": "A language name or code, for example en or en-GB.",
    "pref.style.tone.label": "Tone",
    "pref.style.tone.help": "A few words on how the agent should sound. At most 200 characters.",

    "pref.session.toolset.label": "Abilities in a new terminal chat",
    "pref.session.toolset.help": "Only for chats started in a terminal.",
    "pref.session.persona.label": "Persona in a new terminal chat",
    "pref.session.persona.help": "Only for chats started in a terminal.",

    "pref.autonomy.self_wake.label": "Wake up by itself",
    "pref.autonomy.self_wake.help": "Lets the agent check in on its own. You can turn this off, not on past the server setting.",
    "pref.autonomy.background_review.label": "Look back over its work",
    "pref.autonomy.background_review.help": "Lets the agent review what it did. You can turn this off, not on past the server setting.",

    "pref.outbound.policy.label": "Who the agent may write to",
    "pref.outbound.policy.help": "Anyone, some domains, a list, or nobody. The stricter of this and the server setting wins.",
    "pref.outbound.domains.label": "Domains the agent may write to",
    "pref.outbound.domains.help": "One domain per line. This can only narrow the server list.",
    "pref.outbound.max_new_recipients_per_day.label": "New people per day",
    "pref.outbound.max_new_recipients_per_day.help": "How many people the agent may write to for the first time in a day.",
    "pref.outbound.daily_send_cap.label": "Messages to other people per day",
    "pref.outbound.daily_send_cap.help": "The lower of this and the server limit applies.",
}
