"""The console's vocabulary, in English.

Rules this file is held to by ``tests/unit/webview/test_copy_layer_ratchet.py``,
from ``docs/design/040/README.md`` rule 3 and the 043 copy decisions:

* **No shouting.** No ALL-CAPS label. ``MODEL``, ``TOOLS``, ``VISION`` and
  ``AGENTS`` are the current product's own tells.
* **No meta strings.** No ``A · B · C``. A person reads a sentence, not a
  status bar.
* **No machine names in default copy.** No flag, no function, no proposal id,
  no session hash. All of it stays one disclosure away, never on the first
  screen.
* **First person.** Rob speaks as itself: *I could not read your Base wallet*,
  not *the wallet read failed*.
* **Monospace is for values only**, which is a template decision, not a copy
  one — so no value is ever baked into a sentence here. Numbers arrive as
  ``{placeholders}``.

Keys are ``<screen>.<thing>``. ``shell.*`` and ``nav.*`` belong to the frame
every screen shares.
"""

STRINGS = {
    # ---------------------------------------------------------------- the frame
    "shell.skip": "Skip to content",
    "shell.search": "Search and commands",
    "shell.title": "{page}",

    # The five destinations. Five is the ceiling for a bottom bar, so this list
    # is a mobile-viability constraint and not a matter of taste.
    "nav.new": "New",
    "nav.inbox": "Inbox",
    "nav.work": "Work",
    "nav.money": "Money",
    "nav.agent": "Agent",

    # The badge is aria-hidden; these are what a screen reader announces, so
    # they must never be more certain than the list behind them.
    "nav.inbox_waiting_one": "Inbox, 1 waiting",
    "nav.inbox_waiting": "Inbox, {count} waiting",
    "nav.inbox_uncertain_one": "Inbox, at least 1 waiting, one list unreadable",
    "nav.inbox_uncertain": "Inbox, at least {count} waiting, one list unreadable",
    # …and when it is more than one. Saying "one list" over three of them is a
    # small lie in the direction of "it is fine", which is the direction this
    # badge may never lean.
    "nav.inbox_uncertain_one_lists": (
        "Inbox, at least 1 waiting, {lists} lists unreadable"
    ),
    "nav.inbox_uncertain_lists": (
        "Inbox, at least {count} waiting, {lists} lists unreadable"
    ),

    # --- the head truth: the pause half ------------------------------------- #
    # One sentence made of real numbers, in Rob's voice. It is the first line of
    # every screen, so it is the one line that may never be confidently wrong.
    "shell.logout": "Log out",
    "shell.state.running": "Rob is running.",
    # The in-progress count (running goals + cron runs + live sessions, from the
    # ONE live reader). Absent when nothing is measured as running — never a 0.
    "shell.state.running_busy": "Rob is running, {count} in progress.",
    "shell.state.paused": "Rob is paused.",
    "shell.state.paused_until": "Rob is paused until {until}.",
    # The pause record is read fail-CLOSED: unreadable IS paused, because that
    # is what the runtime then does. Say both halves.
    "shell.state.paused_unreadable": (
        "Rob is paused, because I could not read the pause record and I stop "
        "rather than guess."
    ),
    "shell.state.unknown": (
        "I cannot tell you whether I am running: the pause record did not answer."
    ),
    # A pause has SCOPES (031). A scoped pause rendered as a full stop is the
    # one line that may never be confidently wrong: every other seat says
    # "trading only", so this one may not say "everything".
    "shell.state.paused_scoped": "Rob is paused for {what}.",
    "shell.state.paused_scoped_until": "Rob is paused for {what} until {when}.",

    # When a pause ends. A bare clock reads as "in a few minutes" when it is
    # really tomorrow morning, so the day is part of the answer.
    "shell.when.tomorrow": "{time} tomorrow",
    "shell.when.dated": "{time} on {date}",

    # --- the head truth: the waiting half ----------------------------------- #
    "shell.waiting.none": "Nothing needs you.",
    "shell.waiting.one": "One thing needs you.",
    "shell.waiting.many": "{count} things need you.",
    "shell.waiting.uncertain_none": "Nothing needs you here, and one list is unreadable.",
    "shell.waiting.uncertain_one": "One thing needs you, and one list is unreadable.",
    "shell.waiting.uncertain_many": "{count} things need you, and one list is unreadable.",
    "shell.waiting.uncertain_none_lists": (
        "Nothing needs you here, and {lists} lists are unreadable."
    ),
    "shell.waiting.uncertain_one_lists": (
        "One thing needs you, and {lists} lists are unreadable."
    ),
    "shell.waiting.uncertain_many_lists": (
        "{count} things need you, and {lists} lists are unreadable."
    ),

    # ------------------------------------------------------------- the Chats
    # "Sessions" was one of the old fifteen destinations. It is not a
    # destination: it is the way back to a conversation you already had, which
    # is what the search button is for. So it is an overlay on the frame.
    "chats.title": "Your chats",
    "chats.close": "Close",
    "chats.loading": "Reading your chats.",
    "chats.empty": "No chats yet. Say something below and this is where it will be.",
    "chats.unreadable": (
        "I could not read the list. That is not the same as having none, so I "
        "am not showing you an empty list."
    ),
    "chats.untitled": "A chat with no first line",

    # WHO started it. The difference between a chat you had and a run Rob
    # started by itself at four in the morning; a list that cannot tell them
    # apart is a list you stop opening.
    "chats.creator_owner": "you",
    "chats.creator_cli": "you, in a terminal",
    "chats.creator_api": "a program",
    "chats.creator_cron": "a schedule",
    "chats.creator_goal": "Rob, working on a goal",
    "chats.creator_correspondent": "someone Rob wrote to",
    "chats.creator_unknown": "started by someone I cannot name",

    # Three states, and an unrecognised status is UNKNOWN rather than one of
    # them. A status nobody has seen before is not "done".
    "chats.status_running": "working",
    "chats.status_done": "finished",
    "chats.status_stopped": "stopped",
    "chats.status_unknown": "state unknown",

    # A32 — a row whose live session runs in Rob's own process, not this
    # console. It can be watched from here but not steered from here, and the
    # chip says so rather than letting the row look ordinary.
    "chats.live_at_agent": "live in the agent",

    # ---------------------------------------------------------- the palette
    # One list of everything a person can type, grouped the way the terminal
    # groups it, reachable from every screen. It opens on a slash in the
    # composer or from anywhere on the keyboard. The verbs themselves — their
    # names, groups and one-line help — come from the one verb table, not from
    # here; these are the palette's own frame.
    "palette.title": "Commands",
    "palette.placeholder": "Type a command, or just ask",
    "palette.narrow": "keep typing to narrow it",
    "palette.close": "Close",
    # The text you typed is not a command I know. Pressing enter reads it as a
    # message to me, the same as anything else you type — a command is the short
    # way, never the only way.
    "palette.unknown": (
        "That is not one of my commands. Press enter and I read it as a message."
    ),
    "palette.empty": "Nothing here matches that.",
    "palette.plainwords": (
        "You never have to use one. Ask in plain words and I use my own tools. "
        "A command is the short way, not the only way."
    ),

    # --- read-only ---------------------------------------------------------- #
    # Said once, in the frame, instead of one greyed button at a time.
    "shell.read_only.lead": "You are looking at Rob, not driving it.",
    "shell.read_only.body": (
        "This console is set to look, not touch, so every button that would change "
        "something is off. It says so here once, instead of one greyed button at a time."
    ),

    # -------------------------------------------------------- the destinations
    # Phase 1 places the frame. Each destination says plainly that it is not
    # built yet and where the capability lives meanwhile — never a blank page,
    # never a spinner that never resolves.
    "new.title": "New",
    "new.placeholder": (
        "Chat is coming in the next phase. Until then, talk to me on Telegram, or "
        "run polyrob in a terminal."
    ),

    "inbox.snapshot": "Decisions shown when this page loaded.",
    "inbox.refresh": "Refresh decisions",
    "inbox.title": "Inbox",
    "inbox.placeholder": (
        "The Inbox is coming in the next phase. Until then, the things that need "
        "you reach you on Telegram, and the pending queue lives on the legacy pages."
    ),

    "work.title": "Work",
    "work.placeholder": (
        "Work is coming in the next phase. Until then, goals and cron live on the "
        "legacy pages."
    ),

    "money.title": "Money",
    "money.placeholder": (
        "Money is coming in the next phase. Until then, the book, the positions and "
        "the invoices live on the legacy pages."
    ),

    # ---------------------------------------------------------------- Money › Book
    # The book leads, because on 2026-08-25 the agent published "book flat" while
    # holding three positions and no seat could have shown the owner otherwise.
    # Everything here is drawn client-side by money.js from GET /api/webgate/book.
    "money.sub": (
        "What Rob holds, what it has moved, what it earns, and what running it "
        "costs you. The book comes first, because that is where a wrong number "
        "costs real money."
    ),
    # The five tabs. Only Book is built this phase; Moves, Cash and Invoices are
    # the names of what is coming, and Limits is a link to Agent, where the caps
    # live.
    "money.tab.book": "Book",
    "money.tab.moves": "Moves",
    "money.tab.cash": "Cash",
    "money.tab.invoices": "Invoices",
    "money.tab.limits": "Limits",

    # The verdict, first on the page. Each is a typed value from the reader, and
    # a disagreement or an unverified verdict never reads as agreement.
    "money.book.clean": "The ledger and the chains agree.",
    "money.book.disagreement":
        "Rob's ledger and a chain disagree about what it holds.",
    "money.book.unverified": "I could not verify the book on every chain.",
    "money.book.no_ledger":
        "Rob has not written down a ledger to check against yet.",
    "money.book.checked": "Checked {when}.",
    "money.book.recheck": "Check again",

    # The positions the ledger claims, beside the chain's reading.
    "money.book.holds_title": "What Rob holds",
    "money.book.holds_count": "{count} positions",
    "money.book.col_position": "Position",
    "money.book.col_chain": "Chain",
    "money.book.col_amount": "Amount",
    "money.book.col_worth": "Worth now",
    "money.book.col_since": "Since entry",
    "money.book.entry_at": "bought at {price}",
    "money.book.no_positions":
        "Rob has not written down a position on any chain it can read.",
    "money.book.total_label": "Positions, at today's prices",
    "money.book.total_excludes": "not counted here: {chains}",

    # The chains Rob checked, with the cache age. An unreadable chain is dashed
    # with its reason, never a zero that reads like an empty wallet. Spendable
    # balances live under Cash.
    "money.book.chains_title": "The chains Rob checked",
    "money.book.chains_age": "last read {when}",
    "money.book.chain_read": "Read in full.",
    "money.book.chain_unread": "I could not read this chain.",
    "money.book.chain_unread_why": "Why",

    # The trading run — navigation, not a data claim; it lives in Work.
    "money.book.run_title": "The trading run",
    "money.book.run_aside": "lives in Work",
    "money.book.run_body": (
        "Everything Rob trades lands under Moves; anything it needs from you "
        "lands in the Inbox."
    ),
    "money.book.run_watch": "Watch it in Work",

    # No wallet, on-chain sight off, or nothing to read: the one true thing and
    # the one action, never an empty table and three zeroes.
    "money.book.empty_title": "Rob has no wallet yet.",
    "money.book.empty_body": (
        "Give Rob a wallet and it can hold positions, trade within the limits "
        "you set, be paid for work, and show you the book. Until then this page "
        "has nothing true to say."
    ),
    "money.book.empty_why": "Why",

    # Loading and a read that failed outright.
    "money.book.loading": "Reading the book.",
    "money.book.unreadable": (
        "I could not read the book, so this is not the whole story — it is a "
        "reading that failed, not an empty book."
    ),
    "money.book.unreadable_why": "Why it failed",

    # Relative time, drawn client-side from the reader's own timestamp.
    "money.book.when_now": "just now",
    "money.book.when_min": "{count} min ago",
    "money.book.when_hour": "{count} h ago",
    "money.book.when_day": "{count} d ago",

    "money.book.sources": (
        "The verdict is a typed value, not a regex over the verb's prose — "
        "<b>read_book</b> loops over every money chain and "
        "<b>verdict_from_report</b> derives <b>BookVerdict</b> from the report's "
        "own lists, worst chain wins; the console reads it through <b>api_book</b>. "
        "The positions are the rows <b>read_open_positions</b> parses from the "
        "ledger the agent writes, joined to each chain's reading, over "
        "<b>money_chains</b> plus Solana. Entry and since-entry read a dash with "
        "their reason until the rail-written store lands. A chain that could not "
        "be read is unverified, never zero and never green, and the total names "
        "what it left out. "
        "Moves reads three stores: in flight is <b>open_bridges</b> "
        "(a bridge past its deadline is 'in flight', never 'failed'), with "
        "<b>chain_name_for_id</b> and <b>explorer_url</b> for the origin link; "
        "recent moves are the <b>wallet_spend</b> events; and what Rob has made "
        "is <b>_creations_section</b> over the same events. "
        "Cash is <b>build_ledger</b>'s two blocks — treasury (Rob's cash flow) "
        "and runtime (the owner's compute bill) — which are never summed, and "
        "an unavailable block reads a dash with its reason, never a $0.00. "
        "The wallet identity above those books is the owner-only "
        "<b>wallet_view</b>: public account roles, an identity-bound cached "
        "balance snapshot and unresolved submissions, with no signing action. "
        "Invoices is <b>list_payment_requests</b>, settled by owner attestation "
        "(<b>settle_payment_request</b>) or on-chain by the <b>settlement_watcher</b>; "
        "per-request machine income is undercounted and the page says so. "
        "Limits shows today's used and cap from the same PolicyGate read the "
        "ledger carries, and links to Agent, where the caps are set."
    ),

    # -------------------------------------------------------------- Money › Moves
    # In flight (open bridges), recent moves (wallet_spend), and what Rob has
    # made (creations). Every row that can be linked names its origin chain.
    "money.moves.loading": "Reading what Rob has moved.",
    "money.moves.inflight_title": "In flight",
    "money.moves.inflight_aside": "{count} still moving",
    "money.moves.inflight_empty": "Nothing is bridging right now.",
    "money.moves.inflight_unreadable": "I could not read what is in flight.",
    "money.moves.inflight_unreadable_why": "Why it failed",
    "money.moves.bridge_route": "{origin} to {dest}",
    "money.moves.bridge_line": "{amount}, sent {age} ago and not landed yet.",
    "money.moves.bridge_safe": (
        "Rob will not send it again — a bridge sent twice pays twice — and it "
        "is 'in flight', never 'failed', until it lands or you are told."
    ),
    "money.moves.onchain": "See it on-chain",
    "money.moves.recent_title": "Recent moves",
    "money.moves.recent_empty": "Rob has not moved anything yet.",
    "money.moves.recent_unreadable": "I could not read the recent moves.",
    "money.moves.recent_unreadable_why": "Why it failed",
    "money.moves.recent_partial": "{count} row(s) could not be read, so this may be incomplete.",
    "money.moves.recent_note": (
        "A quote Rob asked for but did not send is not a move. Who decided each "
        "move — Rob under a limit, or you — is not on the record yet."
    ),
    "money.moves.col_when": "When",
    "money.moves.col_what": "What",
    "money.moves.col_chain": "Chain",
    "money.moves.col_amount": "Amount",
    "money.moves.no_amount": "a permission, not a spend",
    "money.moves.amount_unknown": "not recorded",
    "money.moves.made_title": "What Rob has made",
    "money.moves.made_aside": "{count} on-chain",
    "money.moves.made_empty": "Rob has not deployed or launched anything.",
    "money.moves.made_unreadable": "I could not read what Rob has made.",
    "money.moves.made_unreadable_why": "Why it failed",
    "money.moves.made_partial": "{count} row(s) could not be read, so this may be incomplete.",
    "money.moves.col_token": "What",
    "money.moves.col_address": "Address",
    "money.moves.col_cost": "Cost",

    # --------------------------------------------------------------- Money › Cash
    # The two ledgers, NEVER summed. The no-sum line sits exactly where a naive
    # design would put a total.
    "money.cash.loading": "Reading the cash flow.",
    "money.wallet.title": "Wallet",
    "money.wallet.ready": "Wallet ready",
    "money.wallet.public_only": "Public identity only",
    "money.wallet.disabled": "The agent wallet is not enabled.",
    "money.wallet.unavailable": "I could not read the wallet identity.",
    "money.wallet.unavailable_why": "Why it failed",
    "money.wallet.network": "Configured network: {network}",
    "money.wallet.signing_ready": "Signing is available to the guarded agent runtime. This page cannot sign or send.",
    "money.wallet.signing_unavailable": "Signing is not available in this process. Public addresses remain visible.",
    "money.wallet.accounts": "Accounts",
    "money.wallet.balances": "Last known balances",
    "money.wallet.balances_cached": "cached reading",
    "money.wallet.balances_stale": "stale reading",
    "money.wallet.balances_unread": "No trustworthy balance snapshot is available. Unknown is not zero.",
    "money.wallet.col_role": "Role",
    "money.wallet.col_network": "Network",
    "money.wallet.col_address": "Address",
    "money.wallet.col_use": "Use",
    "money.wallet.col_chain": "Chain",
    "money.wallet.col_native": "Native",
    "money.wallet.col_usdc": "USD coin",
    "money.wallet.receive": "Can receive",
    "money.wallet.do_not_fund": "Signing identity only — do not fund",
    "money.wallet.balance_unknown": "not present in the latest trusted snapshot",
    "money.wallet.unaccounted": "Spending is blocked",
    "money.wallet.unaccounted_body": "A submitted transaction has not been accounted for. Resolve it before allowing another spend.",
    "money.cash.income_what": "Income",
    "money.cash.spent_what": "Spent",
    "money.cash.whose": "Rob's own money, last {days} days",
    "money.cash.paid_invoices": "Paid invoices",
    "money.cash.waiting": "Waiting to be paid",
    "money.cash.balance_now": "In the treasury now",
    "money.cash.net": "Left after income and spend",
    "money.cash.no_sum": (
        "Income and Spent are Rob's money as cash flow. Open positions are on "
        "the Book, not here. Neither is added to the cost of running Rob below, "
        "and there is no single number that combines them — there is nothing "
        "sensible to net a compute bill against."
    ),
    # Money that was TAKEN for something never delivered. Neither income nor a
    # pending invoice, so it belongs to neither book — and the console was the
    # one seat that never said it, while the terminal ledger always has.
    "money.cash.refund_due": (
        "{amount} is owed back across {count} payment(s) Rob took and did not "
        "deliver on. Settle or refund each one from the terminal."
    ),
    "money.cash.runtime_title": "What it costs you to run Rob",
    "money.cash.runtime_aside": "your money, not Rob's",
    "money.cash.runtime_window": "Last {days} days",
    "money.cash.runtime_total": "Since the start",
    "money.cash.runtime_calls": "over {count} model calls",
    "money.cash.provider_left": "Left with your provider",
    "money.cash.provider_why": (
        "a balance is a network read; an unknown reads a dash, never $0.00"
    ),
    "money.cash.unreadable": "I could not read the cash flow.",
    "money.cash.unreadable_why": "Why it failed",
    "money.cash.dash_why": "this reading is not trustworthy right now",

    # ----------------------------------------------------------- Money › Invoices
    # Who owes Rob money, and the A20 machine-income gap said on the page.
    "money.invoices.loading": "Reading the invoices.",
    "money.invoices.title": "Who owes Rob money",
    "money.invoices.aside": "{amount} outstanding",
    "money.invoices.empty": "No one owes Rob money yet.",
    "money.invoices.unreadable": "I could not read the invoices.",
    "money.invoices.unreadable_why": "Why it failed",
    "money.invoices.col_who": "Who",
    "money.invoices.col_for": "For",
    "money.invoices.col_amount": "Amount",
    "money.invoices.col_state": "State",
    "money.invoices.state_pending": "unpaid",
    "money.invoices.state_completed": "paid",
    "money.invoices.state_expired": "expired",
    "money.invoices.late": "{count} days late",
    "money.invoices.settle": "Mark paid",
    "money.invoices.settle_done": "marked paid",
    "money.invoices.settle_failed": "The console could not reach the server.",
    "money.invoices.note": (
        "Rob watches the treasury address and marks an invoice paid on its own "
        "when the money arrives. You only step in to chase or to write one off."
    ),
    "money.invoices.machine_title": "Machine payments are not counted here yet.",
    "money.invoices.machine_body": (
        "Payments a machine makes per request land under the payer's name, not "
        "yours, so this page can undercount what Rob earned. The money is not "
        "lost; the page cannot see it until that is fixed."
    ),

    # ------------------------------------------------------------- Money › Limits
    # Today's used/cap from the ledger, and a link to Agent, where they are set.
    "money.limits.loading": "Reading the limits.",
    "money.limits.title": "Today's spend limit",
    "money.limits.used": "{used} of {cap} used today",
    "money.limits.left": "{left} left",
    "money.limits.no_cap": "No daily cap is set — aggregate spend is unbounded.",
    "money.limits.unknown": "The daily cap could not be read.",
    "money.limits.unknown_why": "Why",
    "money.limits.body": "The limits Rob spends within live in Agent, where you set them.",
    "money.limits.link": "Open Agent",

    "agent.title": "Agent",
    "agent.placeholder": (
        "Agent is coming in the next phase. Until then, settings, identity and "
        "diagnostics live on the legacy pages."
    ),

    # What the Agent destination's write actions say back (043 AB2). An edit to
    # what Rob has learned about itself is always a proposal you review, never a
    # change it makes to itself unwatched; a note it keeps is plainer still.
    "agent.identity.saved": "I saved that as a proposal for you to review.",
    "agent.identity.empty": "There was nothing to save.",
    "agent.identity.rejected": (
        "I could not save that — it did not pass the safety check."
    ),
    "agent.memory.added": "I saved that to my notes.",
    "agent.memory.add_failed": (
        "I could not save that — it may be empty or over the limit."
    ),
    "agent.memory.forgotten": "I forgot that note.",
    "agent.memory.not_found": "I could not find that note.",
    "agent.memory.unavailable": "My memory is not available right now.",
    # ⚠️ "I am not set up to remember" and "I could not read what I remember"
    # are DIFFERENT answers, and the knowledge readers used to send the first
    # one in the `error` field, so the console drew it as a failed read. This is
    # the first one, and it rides in `reason` beside an honest empty list.
    "agent.memory.not_configured": (
        "There is no memory backend configured, so there is nothing to read here."
    ),

    # ----------------------------------------------------------- the Agent screen
    # Six tabs of one screen (043 WS-AF1). Everything below the subnav is drawn
    # client-side by agent.js from the tenant-scoped Agent readers; the four
    # posture axes ride as data and render once, as four plain sentences.
    "agent.sub": "Who Rob is, what it can do, and the rules it runs under.",
    "agent.tab_overview": "Overview",
    "agent.tab_identity": "Identity",
    "agent.tab_capabilities": "Capabilities",
    "agent.tab_memory": "Memory",
    "agent.tab_settings": "Settings",
    "agent.tab_advanced": "Advanced",
    "agent.unreachable": "That did not reach the console.",
    "agent.when_now": "just now",
    "agent.when_min": "{count} min ago",
    "agent.when_hour": "{count} h ago",
    "agent.when_day": "{count} d ago",

    # Overview — the ranked health block leads, then the face, then the four
    # posture axes, then what Rob is connected to.
    "agent.ov_health_title": "Health",
    "agent.ov_health_aside": "what Rob checks, and what needs you",
    "agent.ov_health_unreadable": "I could not run the health check.",
    "agent.ov_health_unreadable_why": "Why",
    "agent.ov_health_ok_title": "Everything else checked out",
    "agent.ov_health_none": "Everything checked out",
    "agent.ov_health_ok_body": (
        "Memory, the goal board, scheduled runs, the wallet, the chat surfaces "
        "and the apps all answered."
    ),
    "agent.ov_report_link": "See the full report",
    "agent.ov_unverified": "I could not check these: {sources}",
    "agent.ov_identity_no_avatar":
        "Rob has no face yet. You can make one from the terminal.",
    "agent.ov_posture_title": "What Rob is allowed to do",
    "agent.ov_posture_aside": "four rules, in plain words",
    "agent.ov_axis_local_title": "Treat this machine as yours alone",
    "agent.ov_axis_local_why":
        "Turns on the tools a single owner wants. Never on a shared server.",
    "agent.ov_axis_mode_title": "Act, and tell you after",
    "agent.ov_axis_mode_why": (
        "For everyday actions Rob acts and reports. Money and self-changes "
        "always still ask."
    ),
    "agent.ov_axis_loop_title": "Run background work on its own",
    "agent.ov_axis_loop_why":
        "Rob starts goals, wakes itself and keeps its own schedule.",
    "agent.ov_axis_compute_title": "Reach the computer it runs on",
    "agent.ov_axis_compute_why": "Run a shell, install things, restart itself.",
    "agent.ov_axis_compute_locked": (
        "This one is set when Rob starts and cannot change while it runs. It is "
        "deliberate."
    ),
    "agent.ov_connected_title": "Connected to",
    "agent.ov_models_label": "Models",
    "agent.ov_memory_label": "Where memory is kept",
    "agent.ov_change": "Change",

    # Identity — the persona you wrote (read-only, frozen) and what Rob has
    # learned about itself (a reviewable change; an edit lands in review, never live).
    "agent.id_loading": "Reading the identity.",
    "agent.id_unreadable": "I could not read the identity.",
    "agent.id_unreadable_why": "Why",
    "agent.id_face_title": "Rob's face",
    "agent.id_face_body": (
        "A face is generated once from Rob's own identity and then kept, so it "
        "stays the same everywhere you see it."
    ),
    "agent.id_reroll": "Make a new face",
    "agent.id_reroll_done": "I made a new face.",
    "agent.id_reroll_failed": "I could not make a new face.",
    "agent.id_persona_title": "How Rob should behave",
    "agent.id_persona_aside": "written by you",
    "agent.id_persona_empty": "You have not written a persona yet.",
    "agent.id_learned_title": "What Rob has learned about itself",
    "agent.id_learned_aside": "written by Rob, approved by you",
    "agent.id_learned_empty": "Rob has not written anything about itself yet.",
    "agent.id_edit": "Edit",
    "agent.id_edit_hint": (
        "This is a proposal. It waits in review, and it never takes effect until "
        "you keep it."
    ),
    "agent.id_edit_save": "Save it",
    "agent.id_edit_cancel": "Cancel",
    "agent.id_edit_saved": "I saved that as a proposal for you to review.",
    "agent.id_edit_failed": "I could not save that.",

    # Capabilities — skills, tools and connected services as one list, plus the
    # read-only helpers.
    "agent.cap_loading": "Reading what Rob can do.",
    "agent.cap_unreadable": "I could not read what Rob can do.",
    "agent.cap_unreadable_why": "Why",
    "agent.cap_list_title": "Everything Rob can do",
    "agent.cap_list_aside": "{count} shown",
    "agent.cap_part_unreadable": "One list could not be read.",
    "agent.cap_part_unreadable_why": "Why",
    "agent.cap_empty": "Nothing matches.",
    "agent.cap_col_what": "What it can do",
    "agent.cap_col_from": "Where it came from",
    "agent.cap_col_last": "Last used",
    "agent.cap_money_note": "Spends real money",
    "agent.cap_source_default": "Built in",
    "agent.cap_source_builtin": "Built in",
    "agent.cap_source_optional": "Available to turn on",
    "agent.cap_source_profile": "A helper you shaped",
    "agent.cap_source_mcp": "A connected service",
    "agent.cap_source_skill": "A procedure",
    "agent.cap_source_kept": "Rob wrote it, you kept it",
    "agent.cap_state_on": "on",
    "agent.cap_state_off": "off",
    "agent.cap_helpers_title": "Helpers",
    "agent.cap_helpers_aside": "workers you can read",
    "agent.cap_helpers_unreadable": "I could not read the helpers.",
    "agent.cap_helpers_unreadable_why": "Why",
    "agent.cap_helpers_empty": "Rob has no named helpers yet.",
    "agent.cap_chip_all": "Everything",
    "agent.cap_chip_on": "On",
    "agent.cap_chip_off": "Off",
    "agent.cap_chip_money": "Spends money",
    "agent.cap_chip_helpers": "Never for helpers",
    "agent.cap_search": "Search everything Rob can do",

    # Memory — your notes (add and forget here), what Rob works out on its own,
    # and what it has read.
    "agent.mem_loading": "Reading what Rob remembers.",
    "agent.mem_forget": "Forget",
    "agent.mem_forgotten": "I forgot that note.",
    "agent.mem_forget_failed": "I could not forget that note.",
    "agent.mem_notes_title": "What Rob remembers about you",
    "agent.mem_notes_aside": "most recent notes; older notes are not included in this search",
    "agent.mem_notes_unavailable": "My memory is not available right now.",
    "agent.mem_notes_unavailable_why": "Why",
    "agent.mem_notes_error": "I could not read your notes.",
    "agent.mem_notes_error_why": "Why",
    "agent.mem_notes_empty": "Rob has no notes about you yet.",
    "agent.mem_add_open": "Tell Rob something to remember",
    "agent.mem_add_title": "Tell Rob something to remember",
    "agent.mem_add_aside": "it keeps this one as you type it",
    "agent.mem_add_save": "Remember this",
    "agent.mem_add_cancel": "Cancel",
    "agent.mem_add_hint": (
        "This goes into your notes, kept on this machine. Rob keeps it word for "
        "word and can use it in every chat from now on."
    ),
    "agent.mem_add_done": "I saved that to my notes.",
    "agent.mem_add_failed": "I could not save that.",
    "agent.mem_recall_title": "What Rob works out on its own",
    "agent.mem_recall_aside": "separate from your notes",
    "agent.mem_recall_error": "I could not read what Rob worked out.",
    "agent.mem_recall_error_why": "Why",
    "agent.mem_recall_empty": "Rob has not worked anything out yet.",
    "agent.mem_kb_title": "What Rob has read",
    "agent.mem_kb_aside": "{count} sources",
    "agent.mem_kb_unreadable": "I could not read the sources.",
    "agent.mem_kb_unreadable_why": "Why",
    "agent.mem_kb_empty": "Rob has not read any sources yet.",
    "agent.mem_kb_passages": "{count} passages",
    "agent.mem_search": "Search recall and recent notes",

    # Settings — the preferences people actually change, grouped by intent, plus
    # the money limits gathered here from the ledger.
    "agent.set_loading": "Reading the settings.",
    "agent.set_limits_title": "Money limits",
    "agent.set_limits_aside": "Rob cannot go past these",
    "agent.set_limits_unreadable": "I could not read the limits.",
    "agent.set_limits_unreadable_why": "Why",
    "agent.set_limit_no_cap": "No daily cap is set. Spend is unbounded.",
    "agent.set_limit_unknown": "The daily cap could not be read.",
    "agent.set_limit_daily": "Most Rob can spend in a day is {cap}.",
    "agent.set_limit_used": "{used} of {cap} used today.",
    "agent.set_prefs_unreadable": "I could not read the settings.",
    "agent.set_prefs_unreadable_why": "Why",
    "agent.set_prefs_empty": "There are no settings to show.",
    "agent.set_prefs_other": "Other",
    "agent.set_col_what": "Setting",
    "agent.set_col_value": "Now",
    "agent.set_more": "There are more settings underneath these. They are searchable in",
    "agent.set_more_link": "Advanced",

    # Advanced — every setting, search-first (nothing renders until you ask), and
    # the diagnostic report behind one disclosure.
    "agent.adv_idle_title": "Search, or pick a group.",
    "agent.adv_idle_body": (
        "There are hundreds of settings. Showing them all at once is how this "
        "page became a wall of scroll, so it waits for you to say what you are "
        "looking for."
    ),
    "agent.adv_unreadable": "I could not read the settings.",
    "agent.adv_unreadable_why": "Why",
    "agent.adv_no_match": "Nothing matches.",
    "agent.adv_col_name": "Setting",
    "agent.adv_col_what": "What it does",
    "agent.adv_col_default": "Default",
    "agent.adv_guarded": "the console cannot change this",
    "agent.adv_secret": "a secret",
    "agent.adv_diag_title": "Diagnostics",
    "agent.adv_diag_aside": "for when something is wrong",
    "agent.adv_diag_show": "Show the raw report",
    "agent.adv_diag_hide": "Hide the raw report",
    "agent.adv_diag_empty": "The report is empty.",
    "agent.adv_diag_unreadable": "I could not run the report.",
    "agent.adv_diag_unreadable_why": "Why",
    "agent.adv_search": "Search every setting",

    # The one sources note. Every bold name resolves in the tree
    # (tests/unit/webview/test_sources_note.py), and no first-screen word is a
    # flag, a shout or a call form (test_copy_layer_ratchet.py).
    "agent.sources": (
        "Health &mdash; <b>build_status_snapshot</b>, its ranked health block, "
        "the same one every seat renders. The posture axes &mdash; "
        "<b>build_posture_card</b>, as four plain sentences and not two "
        "vocabularies.<br>"
        "Identity &mdash; the persona you wrote is <b>load_self_context</b>, "
        "read-only and frozen; what Rob has learned is <b>load_self_doc</b>, and "
        "every edit goes through <b>SelfContextWriter</b> into review before it "
        "is ever live.<br>"
        "Capabilities &mdash; one list from <b>tool_capabilities</b>, "
        "<b>get_catalog_skills</b> and <b>load_local_mcp_servers</b>; the helpers "
        "are <b>get_profiles_dir</b>, read-only.<br>"
        "Memory &mdash; recall is <b>LocalVectorMemoryProvider</b>, your notes "
        "are <b>note_create</b> and <b>note_archive</b>, and what Rob has read is "
        "<b>kb_list_sources</b>, all tenant-scoped.<br>"
        "Settings &mdash; the preferences are <b>PrefSpec</b>, read through "
        "<b>display_effective</b>, the same store the terminal writes. Advanced "
        "&mdash; the <b>flags_catalog</b>, search-first, and <b>doctor_report</b> "
        "behind a disclosure."
    ),

    # ---------------------------------------------------------------- Work › Log
    # The four Work tabs. Only Log is live this phase; the other three are the
    # names of what is coming, rendered but not yet reachable.
    "work.tab.now": "Now & next",
    "work.tab.schedule": "On a clock",
    "work.tab.apps": "Apps",
    "work.tab.log": "Log",

    # Creating work from the console (043 A5). The owner asking here IS the
    # grant, so what these say back is plain confirmation, not a warning.
    "work.create.goal_ok": "I added it to my work.",
    "work.create.goal_no_title": "A goal needs a title before I can add it.",
    "work.create.goal_duplicate": "I already have a goal much like that one.",
    "work.create.goal_invalid": "I could not add that goal.",
    "work.create.cron_ok": "I put it on the clock.",
    "work.create.cron_no_task": "A scheduled task needs something to do.",
    "work.create.cron_no_schedule": "A scheduled task needs a time to run.",
    "work.create.cron_bad_schedule": "I could not read that schedule.",
    "work.create.tools_shape": "The tools must be a list of names.",
    # 043 A44: a step budget is REFUSED, never clamped — quietly halving it
    # produces a run that stops short for a reason nobody was told.
    "work.create.max_steps_shape": "The step budget has to be a whole number.",
    "work.create.max_steps_range": "The step budget has to be between 6 and 60.",

    "work.sub": (
        "Everything Rob has done, newest first — the work it starts itself, "
        "your chats, what runs on a clock, the money, the messages it sent you, "
        "and what it changed about itself."
    ),

    # The filter chips. "Everything" clears the filter; the rest are the plain
    # words for the classes the stream is read into, keyed by the class itself.
    "work.log.everything": "Everything",
    "work.log.class_goal": "Its own work",
    "work.log.class_cron": "On a clock",
    "work.log.class_money": "Money",
    "work.log.class_message": "Messages to you",
    "work.log.class_system": "Rob itself",
    "work.log.class_tool": "Tools it used",

    # The diagnostics switch — the lines Rob writes for itself, off by default.
    "work.log.diagnostics": "Show diagnostics",
    "work.log.diagnostics_why": (
        "The lines Rob writes for itself — ticks, skips, retries, locks. Off, "
        "because they are about the machine, not about you."
    ),

    # The per-row raw disclosure — the event exactly as it was written.
    "work.log.raw": "Show the raw record",
    "work.log.raw_hide": "Hide the raw record",

    # The table's two columns, and the states.
    "work.log.header_what": "What happened",
    "work.log.header_when": "When",
    "work.log.loading": "Reading the log.",
    "work.log.empty": "Nothing has happened yet, so there is nothing to show.",
    "work.log.unreadable": (
        "I could not read the activity log, so this is not the whole story — "
        "it is a reading that failed, not a day with nothing in it."
    ),
    "work.log.unreadable_why": "Why it failed",

    # Relative time, drawn client-side from each event's own timestamp.
    "work.log.when_now": "just now",
    "work.log.when_min": "{count} min ago",
    "work.log.when_hour": "{count} h ago",
    "work.log.when_day": "{count} d ago",

    "work.log.sources": (
        "One stream over several stores, newest first — <b>webview/activity.py</b> "
        "reads the per-session feed and the durable rows, and its "
        "<b>activity_backfill</b> hands the console one list. The plain classes "
        "are a reading of the event kinds, not a new field, and "
        "<b>core/activity_class.py</b> is that reading. The machine's own lines — "
        "ticks, skips, retries — are what <b>is_diagnostic</b> names, and they "
        "stay behind one switch rather than mixed into the reading. The raw "
        "record is one disclosure away, and it is the event exactly as it was "
        "written, never a prettied copy of it."
    ),

    # ------------------------------------------------------- Work › Now & next
    # Drawn client-side by work-now.js from GET /api/webgate/goals (the goal
    # board) and GET /api/webgate/running (background helpers). What is waiting
    # is counted from status_counts, never from the dispatcher's order.
    "work.now.section_now": "Now",
    "work.now.section_next": "Next",
    "work.now.loading": "Reading what Rob is doing.",
    "work.now.empty_now": "Rob is not running anything right now.",
    # The live actors the board alone cannot show (2026-09-16 audit, B1): a cron
    # job mid-run and a session the shared registry knows is alive.
    "work.now.cron_title": "A scheduled run is in progress.",
    "work.now.session_title": "A chat is in progress.",
    "work.now.open_chat": "Open it",
    "work.now.live_unreadable": "Part of what is running could not be read.",
    "work.now.live_unreadable_why": "Why",
    "work.now.empty_next": "Nothing is waiting its turn.",
    "work.now.disabled": (
        "Rob's own work is switched off, so it starts nothing on its own."
    ),
    "work.now.unreadable": "I could not read the goal board.",
    "work.now.unreadable_why": "Why it failed",
    "work.now.running_count": "{count} running",
    "work.now.next_queued": "{count} queued",
    "work.now.elapsed_started": "started {elapsed} ago",
    "work.now.stop": "Stop",
    "work.now.pause": "Pause",
    "work.now.drop": "Drop it",
    "work.now.retry": "Try it again",
    "work.now.helper_lead": "A helper is doing part of this for Rob.",
    "work.now.helper_body": (
        "Helpers cannot spend money or change your files. It reports back when "
        "it is done."
    ),
    "work.now.helper_goal": "Working on {goal}",
    "work.now.status_ready": "waiting its turn",
    "work.now.status_waiting": "waiting",
    "work.now.status_triage": "being sorted",
    "work.now.status_blocked": "stopped after repeated failures",
    "work.now.unreachable": "The console could not reach the server.",
    # Compact durations, filled client-side from each row's own timestamp.
    "work.now.secs": "{count}s",
    "work.now.mins": "{count}m",
    "work.now.hours": "{count}h",
    "work.now.days": "{count}d",

    # ------------------------------------------------------- Work › On a clock
    # Drawn by work-now.js from GET /api/webgate/cron; Cancel posts to
    # /api/webgate/cron/{id}/cancel — the same verb Telegram's owner uses.
    "work.schedule.loading": "Reading the clock.",
    "work.schedule.empty": "Nothing runs on a clock yet.",
    "work.schedule.disabled": "Nothing runs on a clock here.",
    "work.schedule.unreadable": "I could not read the schedule.",
    "work.schedule.unreadable_why": "Why it failed",
    "work.schedule.header_what": "What it does",
    "work.schedule.header_when": "When",
    "work.schedule.header_last": "Last run",
    "work.schedule.cancel": "Cancel",
    "work.schedule.last_never": "not yet run",
    "work.schedule.ran_ago": "{elapsed} ago",
    "work.schedule.unreachable": "The console could not reach the server.",

    # -------------------------------------------------------------- Work › Apps
    # Everything Rob built you can open, in three tiers by reach: online app
    # services, published pages/files with a link, and files still in a chat
    # folder. Nothing built is the empty state — but only when every store
    # answered and every one was empty.
    "work.apps.loading": "Reading what Rob has built.",
    "work.apps.online_title": "Online",
    "work.apps.online_aside": "{count} in all",
    "work.apps.online_empty": "Rob has not put an app online yet.",
    "work.apps.online_unreadable": "I could not read the app registry.",
    "work.apps.online_unreadable_why": "Why it failed",
    "work.apps.online_open": "Open it",
    "work.apps.status_live": "Online and healthy.",
    "work.apps.status_deploying": "Going online now.",
    "work.apps.status_approved": "Approved — going online.",
    "work.apps.status_pending": "Built and waiting for you. The decision is in the Inbox.",
    "work.apps.status_unhealthy": "Online but not answering.",
    "work.apps.status_failed": "It stopped after repeated failures.",
    "work.apps.status_stopped": "You took this down. The code is still here.",
    "work.apps.status_paused": "Paused. It will go back online when Rob resumes.",
    "work.apps.published_title": "Published",
    "work.apps.published_aside": "pages and files with a link",
    "work.apps.published_empty": "Nothing is published with a link yet.",
    "work.apps.published_unreadable": "I could not read the published pages.",
    "work.apps.published_unreadable_why": "Why it failed",
    "work.apps.col_what": "What",
    "work.apps.col_link": "Link",
    "work.apps.open_link": "Open",
    "work.apps.made_title": "Made, but not shared",
    "work.apps.made_aside": "files from your chats",
    "work.apps.made_empty": "No files are waiting in a chat folder.",
    "work.apps.made_unreadable": "I could not read the workspace files.",
    "work.apps.made_unreadable_why": "Why it failed",
    "work.apps.col_file": "File",
    "work.apps.col_kind": "Kind",
    "work.apps.made_note": (
        "These live in the folder of the chat that made them. Publishing one "
        "gives it a link anyone can open; Rob asks you before it does that."
    ),
    "work.apps.empty_title": "Rob has not built anything yet.",
    "work.apps.empty_body": (
        "Ask it to build something and it will appear here — a running app with "
        "its own address, a page with a link, or a file you can open. Rob always "
        "asks before it puts anything on the public internet."
    ),
    "work.apps.empty_action": "Ask Rob to build something",

    # The ONE sources note for the whole Work screen (043 R11): every tab names
    # what it read, in one paragraph, because a screen names its sources once.
    "work.sources": (
        "One screen over its stores. Now and Next read "
        "<b>GoalBoard.list_recent</b> and <b>status_counts</b> — the newest "
        "rows and the count of every one, never <b>board.list</b>, which is the "
        "dispatcher's order. The helper lines are the background delegations in "
        "<b>AutonomyStateStore.list_running</b>, scoped to you. On a clock is "
        "<b>CronService</b> over its own store. Apps is the "
        "<b>AppServiceRegistry</b> (the <b>app_services</b> store) for what is "
        "online, and the artifact ledger (<b>get_artifact_ledger</b>) for the "
        "published and made-but-not-shared tiers — that ledger is session-scoped, "
        "so those two tiers fill on a per-chat view, not the whole-Work one. Log "
        "is one stream over the per-session feed and the durable rows — "
        "<b>activity_backfill</b> hands the console one list, the plain classes "
        "are a reading of the event kinds, and <b>is_diagnostic</b> names the "
        "machine's own lines, which stay behind one switch. A decision is never "
        "here — it waits in the Inbox."
    ),

    # Every screen names what it read (043 R11) — a number nobody can trace is
    # a number nobody can trust. A destination that is not built yet reads
    # nothing, and says so rather than leaving the line off.
    "placeholder.sources": (
        "This screen reads nothing yet, so it claims nothing. The legacy pages "
        "still serve what it will show."
    ),

    # ------------------------------------------------------------------- Inbox
    # An Inbox item is a durable record, for this tenant, blocked on an owner
    # decision, that Rob cannot resolve alone. All four tests must hold — which
    # is why a late invoice is listed and NOT counted: the settlement watcher
    # closes that one on its own, so it is not a decision.
    "inbox.sub": (
        "What is blocking Rob first, then what expires soonest. It keeps working "
        "on everything else while these wait."
    ),
    "inbox.pill_none": "nothing waiting",
    "inbox.pill_waiting_one": "1 waiting",
    "inbox.pill_waiting": "{count} waiting",
    "inbox.pill_partial_one": "1 waiting, 1 list unread",
    "inbox.pill_partial_one_list": "{count} waiting, 1 list unread",
    "inbox.pill_partial": "{count} waiting, {lists} lists unread",
    "inbox.not_blocking.title": "Not blocking",
    "inbox.not_blocking.aside": "listed, not counted",

    # Card grammar: what Rob wants, why, what yes costs, what no costs, and how
    # long it has waited. Every card answers all five.
    "inbox.waiting_for": "Waiting {age}.",
    "inbox.expires_in": "It expires in {age}, and I will tell you when it does.",
    "inbox.not_stuck": "Here because you may want to act, not because I am stuck.",

    # The honest-state rule in pixels. An unreadable source is an ENTRY with its
    # own dashed rule — never a silent omission, and never a zero.
    "inbox.partial.lead": "This list is incomplete.",
    "inbox.partial.body": (
        "I could not read {source}, so there may be more waiting than what is "
        "below. Decide those on Telegram until this is readable again."
    ),
    "inbox.unreadable.body": (
        "I could not open this list. The store did not answer, so I do not know "
        "whether anything is waiting in it. A dash is not the same as none."
    ),
    "inbox.unreadable.meta": "Last read successfully {age} ago.",
    "inbox.unreadable.retry": "Try again",
    "inbox.unreadable.why": "Why it failed",

    "inbox.empty.title": "Nothing needs you.",
    "inbox.empty.body": (
        "I am working on everything I can reach by myself, and I will stop here "
        "the moment I need you to decide something."
    ),
    "inbox.empty.checked": (
        "I can only say nothing needs you because every source answered. If one "
        "had not, this page would say so instead."
    ),
    "inbox.empty.see_work": "See what it is working on",
    "inbox.empty.history": "What you decided before",

    # Shared card verbs. An item type adds its own; these are the ones more than
    # one card uses. A verb says what it does, never "Confirm" or "Submit".
    "inbox.action.not_now": "Not now",
    "inbox.action.see_details": "See what it does",
    "inbox.action.read_it": "Read it",
    "inbox.action.keep": "Keep it",
    "inbox.action.discard": "Throw it away",
    "inbox.action.publish": "Put it online",
    "inbox.action.chase": "Chase it",
    "inbox.action.write_off": "Write it off",
    # The seat-neutral vocabulary core.surfaces.inbox exposes. A card that has
    # its own better word (an app goes online, a note is kept) overrides these;
    # these are what every other kind falls back to.
    "inbox.action.approve": "Yes",
    "inbox.action.reject": "No",
    "inbox.action.fulfill": "I have done it",
    "inbox.action.show": "Show me",

    # What the page says it read. Five stores answer or refuse, and the page
    # names them either way — that is what makes the count trustworthy.
    "inbox.sources.lead": "Read {sources}.",
    "inbox.sources.all_answered": "All of them answered.",
    "inbox.sources.refused": "Could not read {sources}.",
    "inbox.sources.what_counts": (
        "The count is decisions. An item listed under Not blocking is not one, "
        "because it closes on its own."
    ),
    # The citations. They carry <b> markup and are rendered with |safe, which
    # is why nothing here is ever built from a value a person supplied. Every
    # name is checked against the tree by
    # tests/unit/webview/test_sources_note.py, so a note can never outlive the
    # thing it names.
    "inbox.sources.cites": (
        "Read by <b>core.self_evolution</b>, "
        "<b>list_pending_tool_approvals</b>, <b>pending_correspondent_items</b>, "
        "<b>GoalBoard.asks</b> and the <b>app_services</b> registry, composed by "
        "<b>core.surfaces.inbox</b>."
    ),

    # The endpoint's own answers. Short, and each says what happened rather
    # than that something happened.
    "inbox.owner_console": "app decisions",
    "inbox.ask.decided": "Done. {count} goals can run again.",
    "inbox.ask.decided_one": "Done. One goal can run again.",
    "inbox.ask.decided_none": "Done.",
    "inbox.ask.gone": "That one is no longer open — it was decided elsewhere.",
    "inbox.fulfill.refused": "Only a blocked goal can be marked done that way.",
    # Read-only: the decision buttons are not rendered at all, and the page says
    # WHERE to decide instead — once, under the list, the way the composer does.
    # A line under every card would be the per-control restatement the frame's
    # banner exists to replace.
    "inbox.read_only.decide": (
        "Decide these on Telegram, or run polyrob in a terminal. This console "
        "is set to look, not touch."
    ),
    # The one sentence the script may need that the server never rendered: a
    # click that did not reach the console at all.
    # The console could not even establish WHICH owner it is scoped to. Distinct
    # from "no owner is bound", the next line.
    "inbox.owner_unreadable": "I could not read who this console belongs to.",
    # ⚠️ The READ-path reason. `webgate.UNBOUND_OWNER_MESSAGE` says "a WRITABLE
    # console needs a bound owner", which is untrue of the read-only console
    # this most often fires on — and it names the variable, which the copy rules
    # keep off a first screen. The remedy rides on the API refusal instead
    # (`webview/inbox.py::UNBOUND_REMEDY`), where an operator reads it.
    "inbox.unbound_owner": (
        "This console has no bound owner, so it cannot tell whose queue to read."
    ),
    "inbox.unreachable": (
        "That did not reach me, so nothing was decided. Try again, or decide it "
        "on Telegram."
    ),

    # -------------------------------------------------------------------- chat
    # The frame speaks about Rob in the third person ("Rob is running") because
    # it is the product describing the agent. Rob speaks about itself in the
    # first person. The mockups do exactly this, and the split is the point.
    "chat.title": "Chat",
    "chat.composer.label": "Message Rob",
    "chat.composer.placeholder": "Ask Rob, or tell it what to do",
    "chat.composer.placeholder_busy": "Add something while it works",
    "chat.composer.hint": "Type a slash for commands.",
    "chat.send": "Send",
    "chat.stop": "Stop",
    "chat.steer": "Steer",

    # The transcript's action line is narrated in Python (narrate.py) and shipped
    # on the feed event, so these two are only the fallback for a legacy event
    # that carried neither a narration nor a preview — a name-free line, never a
    # tool id and never a trace token.
    "chat.act.did_work": "Ran a tool",
    "chat.act.failed": "An action did not finish",

    # The cold open is the one place the design spends its boldness: one true
    # sentence made of real numbers, in Rob's voice. No model picker, no tool
    # checkboxes, no step slider.
    "chat.open.sub": (
        "Ask me anything, or tell me what to do. You can say it in plain words, "
        "and I will use my own tools."
    ),
    # The hero's clauses. Assembled in webview/chat_open.py from build_recap's
    # own entries, never from a second reader — /journey and Telegram /recap
    # read the same builder, so the three can never disagree.
    "chat.open.hero": (
        "In the last day I finished {goals}, my running cost {spend}, and {income}."
    ),
    # build_recap emits NO ledger entry for an all-zero rollup AND for a rollup
    # it could not read, so the two are indistinguishable here. Making no claim
    # about money is the honest answer; a confident $0.00 is not.
    "chat.open.hero_no_money": "In the last day I finished {goals}.",
    "chat.open.hero_unreadable": "I could not read what I did in the last day.",
    # ⚠️ The recap annotates a rollup whose legs it could not read (core/recap.py
    # H14b). Dropping that note printed "my running cost $0.00" over a leg that
    # was never read — a confident zero beside a real figure. The money clause is
    # dropped instead, and the recap's OWN reason is carried verbatim inside this
    # sentence, so the reader learns what is missing rather than reading a lie.
    "chat.open.money_unreadable": (
        "I am not showing what it cost or what came in, because {why}."
    ),
    "chat.open.goals_none": "nothing",
    "chat.open.goals_one": "one goal",
    "chat.open.goals_many": "{count} goals",
    "chat.open.income_none": "nothing came in",
    "chat.open.income_some": "{amount} came in",
    "chat.open.starters_label": "Things you could ask",
    "chat.sources": (
        "The sentence above is <b>build_recap</b> over the last day, the same "
        "reader the terminal and the phone use. The count of what needs you is "
        "the Inbox's own sources, composed by <b>core.surfaces.inbox</b>. The "
        "pane beside the chat reads this chat's own folder through "
        "<b>get_workspace_dir</b>, its finished files from <b>core.artifacts</b> "
        "(the <b>ArtifactLedger</b>, each with its verdict), and its timeline "
        "from the session feed, <b>add_to_feed</b>, one line per action."
    ),

    "chat.starter.away": "What did you do while I was away?",
    "chat.starter.book": "What is the book worth right now?",
    "chat.starter.cost": "What did that cost?",
    "chat.starter.pause": "Pause trading until Monday",

    "chat.receipt.working": "Working {elapsed}, {actions} actions so far, {cost}.",
    "chat.receipt.done": "Worked {elapsed}, took {actions} actions, cost {cost}.",
    "chat.receipt.stopped": (
        "Stopped after {elapsed}, having taken {actions} actions, cost {cost}."
    ),
    # A cost the transcript has not read yet is a dash, never a confident $0.00.
    "chat.receipt.cost_unknown": "—",

    # A32 — a Steer that came back 409: this chat is live in Rob's OWN process,
    # not this console, so the console cannot steer it from here. It is an honest
    # state, not a failure — it names the process, tells you to watch below, and
    # gives the retry hint. {pid} is the owning worker's process id.
    "chat.live_at_agent": (
        "This chat is live in Rob's own process (pid {pid}), so I cannot steer "
        "it from this console. Watch it below, and try again in a moment, or "
        "steer it from the terminal."
    ),

    # A failure names its LAYER, says what it does NOT mean, gives the part that
    # did work, and offers the recovery. The trace stays behind one disclosure.
    # This is the answer to the terminal's 242-line traceback.
    "chat.failed.not_your_money": (
        "This is the network between me and the chain, not your wallet and not "
        "your money."
    ),
    "chat.failed.no_total": (
        "I am not showing you a total, because a total I cannot verify is worse "
        "than none."
    ),
    "chat.failed.partial": "What I could read: {what}.",
    "chat.failed.retry": "Try again",
    "chat.failed.trace": "What the node returned",

    # ⚠️ The ownership PROBE failed. That is not "this session does not exist",
    # which is what a 404 says — and it is not "you may look", which is what
    # rendering the thread would say. Both would be confident; only one of them
    # would be right, and nothing here knows which.
    "chat.ownership_unknown": (
        "I could not check who owns this session, so I am not showing it. That "
        "is not the same as it being gone."
    ),
    "chat.ownership_unknown_note": (
        "Try again in a moment. If it keeps happening, the chat is on Telegram "
        "and in a terminal."
    ),

    "chat.read_only.composer": "This console is set to look, not touch.",
    "chat.read_only.note": (
        "Talk to me on Telegram, or run polyrob in a terminal. To let this "
        "console act, turn read-only off in Agent, under Settings."
    ),

    # ------------------------------------------------------------ the Work pane
    # Beside the chat on a bound session: what this chat MADE (Files, three
    # tiers) and what it DID (Timeline). Read-only. workpane.js reads these off
    # #workpane-copy's data attributes, the way chats.js reads its own.
    "workpane.files_title": "Files",
    "workpane.files_aside": "this chat's folder",
    "workpane.files_note": (
        "These live with this chat. Rob can send them to you, put them online "
        "or publish them, and it asks you first every time."
    ),
    "workpane.files_empty": "This chat has not made or been given any files yet.",
    "workpane.files_unreadable": "I could not read this chat's folder.",
    # One Files source failed while the other read fine. Naming which one is what
    # keeps a half-read from reading as "nothing here".
    "workpane.files_tree_unreadable": (
        "I could not read this chat's folder, so I am showing only what it "
        "recorded as finished."
    ),
    "workpane.files_ledger_unreadable": (
        "I could not read the record of what this chat finished, so I cannot "
        "tell which of these are ready for you."
    ),
    "workpane.tier_folder": "In this chat's folder",
    "workpane.tier_folder_why": "everything it holds right now",
    "workpane.tier_ready": "Ready for you",
    "workpane.tier_ready_why": "Finished things",
    "workpane.tier_working": "Working files",
    "workpane.tier_working_why": "What Rob made to do the job, not for you",
    "workpane.tier_given": "Things you gave Rob",
    "workpane.tier_given_why": "What you handed it",
    "workpane.verdict_ok": "ready",
    "workpane.verdict_changed": "changed since it was made",
    "workpane.verdict_missing": "no longer there",
    "workpane.verdict_unknown": "not checked",
    "workpane.timeline_title": "Timeline",
    "workpane.timeline_note": "Every line is one action, newest last.",
    "workpane.timeline_empty": "Nothing has happened in this chat yet.",
    "workpane.timeline_unreadable": "I could not read what this chat did.",
    "workpane.timeline_count": "{count} actions",
    "workpane.loading": "Reading this chat's work.",
}

# Shared settings controls; policy explanations originate in the server payload.
STRINGS.update({
    "agent.control_saved": "Saved. The effective value is shown above.",
    "agent.control_queued": "Removal requested for review; the effective value stays in place:",
    "agent.control_save": "Save",
    "agent.control_confirm": "Confirm this change",
    "agent.control_cancel": "Cancel",
    "agent.control_confirmation": "This setting changes a guarded policy. Review the value before confirming.",
    "agent.control_effective": "Effective value",
    "agent.control_list": "One entry per line",
    "agent.control_operator": "Managed by the operator. Change it from the local command line.",
    "agent.control_owner": "Changing this setting requires the owner console.",
    "agent.control_readonly": "This console is read-only.",
    "agent.control_pattern": "This is a naming pattern, not an individual setting.",
    "agent.control_restart": "Saved configuration applies after a service restart. The running value is shown above.",
})

STRINGS.update({"chat.history_unavailable": "Chat history could not be read. Reload to retry.", "chat.live_unavailable": "Live updates are unavailable. Reconnecting…", "chat.action_unavailable": "The request did not reach the console. Your text is still here."})

STRINGS.update({
    "work.create.goal_label": "Create a goal",
    "work.create.cron_label": "Schedule work",
    "work.create.title_label": "What should get done?",
    "work.create.schedule_label": "When should it run?",
    "work.create.schedule_hint": "Use a duration such as 30m, a repeating schedule such as every 1h, or a cron expression.",
    "work.create.submit": "Create",
    "work.create.unreachable": "The request did not reach the console. Your draft is still here.",
})

STRINGS.update({"chats.more": "Load older chats"})

# --- 2026-09-21 interface audit, section A (console) -------------------------- #
# One block, appended: the words the findings below need. Each row names the
# seat a person can actually reach, never a flag.
STRINGS.update({
    # A2 — the pause headline names a control; this is that control.
    "shell.pause": "Pause",
    "shell.resume": "Resume",
    "shell.pause_label": "Pause or resume background work",
    "shell.pause_unreachable": "The request did not reach the console.",
    # A32 — the activity room refused this seat; say so instead of going quiet.
    "shell.live_refused": "Live updates are not available for this seat.",

    # A1 — Rob's face on Agent, read from the record the avatar store writes.
    "agent.ov_identity_kept": "Kept. This face and this voice are permanent.",
    "agent.ov_identity_draft": "A draft. Keep it under Identity to make it permanent.",
    "agent.ov_identity_traits": "Traits: {traits}",
    "agent.ov_identity_voice": "Voice: {voice}",
    "agent.ov_identity_made": "Made {when}",
    "agent.ov_identity_instance": "Instance {instance}",
    "agent.ov_identity_unreadable": "There is a face here, but I could not read what it is made of.",
    "agent.ov_identity_unreadable_why": "Why",

    # A19 — the keep ceremony, which had no seat in the console at all.
    "agent.id_keep": "Keep this face",
    "agent.id_keep_hint": "Keeping is permanent. Nothing can change the face or the voice afterwards.",
    "agent.id_keep_done": "Kept. This is the face from now on.",
    "agent.id_keep_failed": "I could not keep the face.",
    "agent.id_kept_title": "Kept for good",
    "agent.id_kept_body": "The face and the voice are permanent. There is nothing left to decide here.",

    # A26 — connected services are an inventory here, and the label says where
    # the verbs live.
    "agent.cap_mcp_note": (
        "Connected services are listed here only. Add or remove one from a "
        "terminal with polyrob, or on Telegram with /mcp."
    ),

    # A6 / A37 — a count over a partial read is a floor, and it says so.
    "work.now.running_count_partial": "at least {count} running",

    # A22 — the app registry's own verbs, which had no console caller.
    "work.apps.kill": "Stop it",
    "work.apps.logs": "Show the log",
    "work.apps.logs_hide": "Hide the log",
    "work.apps.logs_empty": "The log is empty.",
    "work.apps.logs_unreadable": "I could not read the log.",
    "work.apps.unreachable": "The request did not reach the console.",

    # A13 / A11 — rows the tenant filter removed are named, never dropped.
    "work.log.filtered_out": "{count} rows belong to another tenant and are not shown.",
    "work.log.raw_switch": "Include the raw record",
    "work.log.raw_switch_why": "Each line can then be opened exactly as it was written.",

    # A44 — the create forms carry the fields the writers already accept.
    "work.create.body_label": "Anything else it should know?",
    "work.create.priority_label": "How important is it?",
    "work.create.priority_hint": "A higher number is served first.",
    "work.create.tools_label": "Which tools may it use?",
    "work.create.tools_hint": "One name per line. Leave it empty and I choose.",
    "work.create.max_steps_label": "How many steps may it take?",
    "work.create.max_steps_hint": "Between 6 and 60. Leave it empty for my own default.",
    "work.create.deliver_label": "Where should the result go?",
    "work.create.deliver_none": "Keep it to myself",
    "work.create.deliver_telegram": "Telegram",
    "work.create.deliver_email": "Email",
    "work.create.deliver_twitter": "X",
    "work.create.deliver_target_label": "Who should receive it?",
    "work.create.deliver_target_hint": "Leave it empty and I use the owner address I already have.",
    "work.create.wake_agent_label": "Wake me for this run",

    # A4 — the liquidity pane went blank on any refusal.
    "money.liquidity.unreadable": "I could not read the liquidity book.",
    "money.liquidity.unreadable_why": "Why",
    # A35 — the outstanding total is the server's, over every row.
    "money.invoices.truncated": "There are more invoices than the ones shown here.",
    "money.invoices.unpriced": "{count} outstanding invoices carry no amount I could read, so they are not in that total.",
    # A25 — Money shows what the money verbs did; it is not where they are run.
    "money.moves.reach": (
        "Bridging, launching and deploying run from a terminal with polyrob "
        "wallet, or on Telegram with /bridge, /launch and /deploy. This page "
        "shows what they did."
    ),

    # A18 — the palette off a chat screen said nothing and closed.
    "palette.reach": "Type this into a chat to run it.",

    # A27 — an ask needs an answer, not only a yes and a no.
    "inbox.answer_label": "Your answer",
    "inbox.answer_placeholder": "Type your answer",

    # An app decision changes the whole instance, so a tenant seat may not take
    # it. The card still SHOWS what is waiting — hiding it would be the other
    # failure — and names the seat that can decide instead of drawing two
    # buttons that refuse.
    "inbox.decide_owner_console": (
        "This one changes the whole instance, so it is decided on the owner's "
        "own console, or with polyrob apps approve in the terminal."
    ),

    # A24 — the composer's file picker, which the README described and the
    # template never carried.
    "chat.attach": "Attach a file",
    "chat.attach_sending": "Sending {name}",
    "chat.attach_done": "{name} is in this chat's folder.",
    "chat.attach_failed": "I could not take {name}.",
    "chat.attach_unreachable": "The file did not reach the console.",

    # E14 — a read-only console refuses owner verbs; the chat box says so.
    "chat.read_only.verbs": (
        "Owner commands typed here are refused too. Run them on Telegram, or "
        "from a terminal with polyrob."
    ),
})
