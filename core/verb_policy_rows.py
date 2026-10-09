"""067 P1: the core rows of the per-ACTION policy table (DATA ONLY).

Read by ``core/verb_policy.py`` and nothing else: import the registry, not this
module. No imports here, so the data can never pull a tier into the policy seam.

Keys: owning tool id (``None`` = an action registered directly on the Controller)
-> EXACT emitted action name -> the non-default :class:`core.verb_policy.VerbPolicy`
fields. A name absent here has every default (effect ``inherit``: the tool
ceiling decides). Every list that used to hold these names is now a derived view
of this table; the per-verb rationale that lived beside those lists lives here.

⚠️ ``effect="none"`` SILENCES a verb: telemetry and the pause gate both skip it,
so every such row must be a read or a workspace-local act.
"""

CORE_VERB_ROWS = {
    None: {
        # correspondent_blocked on a tool-less action: no owning tool_id, so tool-id resolution
        # can NEVER cover it; the name row is the only gate.
        # room_denied (044): a room turn may never call it, whoever spoke: deferred execution,
        # cross-session recall, owner state, outbound to other targets, money-adjacent reads,
        # self-modification, control.
        "message": dict(effect="comms", correspondent_blocked=True, room_denied=True),
        "skill_manage": dict(effect="self", correspondent_blocked=True, room_denied=True),
        "self_context_manage": dict(effect="self", correspondent_blocked=True, room_denied=True),
        # owner.md: the richest PII target in the process plus a write path; its own forged-turn
        # guard keys on is_sub_agent/leaf/turn_kind, none of which correspondent injection sets.
        "owner_doc_manage": dict(effect="self", correspondent_blocked=True, room_denied=True),
        "agent_avatar": dict(effect="self", correspondent_blocked=True, room_denied=True),
        "preferences": dict(effect="self", correspondent_blocked=True, room_denied=True),
        "mcp_install": dict(
            effect="self", approval=("recommended", "always_queued"), correspondent_blocked=True,
            room_denied=True,
        ),
        # A self-service capability expansion.
        # A tainted turn must not widen the surface the next (untainted) turn inherits.
        "load_tool": dict(effect="self", correspondent_blocked=True, room_denied=True),
        # The owner lane: core/surfaces/user_delivery.py records send_message and done as
        # user_delivery. Classifying them again would double every owner reply.
        "send_message": dict(effect="none"),
        "done": dict(effect="none"),
        # effect=none from here to room_read in this group: internal or read-only.
        # Delegation (delegate_task/subtask/parallel_subtasks), identity and self-evolution
        # (skill_manage/self_context_manage/worker_manage), tenant preferences (owner-UX P2 T2)
        # and the gated outbound message: a tainted session reaches none of them.
        "delegate_task": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "subtask": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "parallel_subtasks": dict(effect="none", correspondent_blocked=True, room_denied=True),
        # 031: a tainted turn must not RESUME autonomy; a third party steering the pause state
        # at all is not the owner driving.
        "autonomy_control": dict(effect="none", correspondent_blocked=True, room_denied=True),
        # H04: question= speaks to the owner AS Rob, answer= writes what a later run reads as
        # the owner's decision.
        "owner_ask": dict(effect="none", correspondent_blocked=True),
        # Action cards (2026-09-27): a question with buttons, and a money PROPOSAL whose only
        # action is the verb's quote on the owner's seat. Neither moves anything; both speak to
        # the owner as Rob, so a tainted or room turn reaches neither.
        "present_choice": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "propose_action": dict(effect="none", correspondent_blocked=True, room_denied=True),
        # Who we talked to and what was said (gated on CORRESPONDENT_ACCESS_ENABLED, so live
        # wherever taint exists); session_search/memory_search/recent_activity: past sessions
        # and the run ledger with per-run spend. With the D1 reply exemption on, what they
        # return could be echoed to the tainting party.
        "contact_history": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "session_search": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "memory_search": dict(effect="none", correspondent_blocked=True, room_denied=True),
        # P1-4: a curated-memory write persists into FUTURE prompts (injection persistence); the
        # whole action, read too, fails closed.
        "memory": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "recent_activity": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "insights": dict(effect="none", room_denied=True),
        # I-6: reveals wallet balance + tenant ledger (usage_summary: cost data and invoice-
        # draft suggestions, a social-engineering target).
        "agent_status": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "usage_summary": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "load_skill": dict(effect="none"),
        "read_skill_resource": dict(effect="none"),
        "tool_search": dict(effect="none"),
        # The group ledger holds the owner's own room lines: the disclosure class of
        # contact_history.
        "room_read": dict(effect="none", correspondent_blocked=True),
        # Mute/ban/delete a member: an act on an identified third party. Never from a room turn
        # (a member line must not steer a ban) nor a tainted session.
        "room_moderate": dict(effect="comms", correspondent_blocked=True, room_denied=True),
        "worker_manage": dict(correspondent_blocked=True),
    },
    "app_service": {
        "app_service_list_apps": dict(effect="none"),
        "app_service_logs": dict(effect="none"),
        "app_service_deploy": dict(effect="public"),
        "app_service_stop": dict(effect="public"),
    },
    "browser": {
        # Navigation and reading are read egress (the same class as web_fetch). Only the verbs
        # that type, click or submit carry an agent-controlled payload (network).
        "browser_extract_page_content": dict(effect="none"),
        "browser_get_dropdown_options": dict(effect="none"),
        "browser_go_back_action": dict(effect="none"),
        "browser_go_to_url": dict(effect="none"),
        "browser_open_tab": dict(effect="none"),
        "browser_refresh_page": dict(effect="none"),
        "browser_scroll": dict(effect="none"),
        "browser_scroll_down": dict(effect="none"),
        "browser_scroll_to_text": dict(effect="none"),
        "browser_scroll_up": dict(effect="none"),
        "browser_search_google": dict(effect="none"),
        "browser_switch_tab": dict(effect="none"),
        "browser_click_element": dict(effect="network"),
        "browser_input_text": dict(effect="network"),
        "browser_send_keys": dict(effect="network"),
        "browser_select_dropdown_option": dict(effect="network"),
    },
    "code_execution": {
        # P1-4: ⚠️ NAMESPACED. The method run_code registers as code_execution_run_code; the
        # bare name matched nothing, so tool-id resolution was the single point of failure.
        "code_execution_run_code": dict(effect="code", correspondent_blocked=True),
    },
    "coding": {
        # Workspace-local file edits; only running the tests executes code.
        "coding_apply_patch": dict(effect="none"),
        "coding_create_file": dict(effect="none"),
        "coding_delete_file": dict(effect="none"),
        "coding_grep": dict(effect="none"),
        "coding_move_file": dict(effect="none"),
        "coding_restore": dict(effect="none"),
        "coding_snapshots": dict(effect="none"),
        "coding_str_replace": dict(effect="none"),
        "coding_run_tests": dict(effect="code"),
    },
    "cronjob": {
        # room_denied, deferred execution (as goal).
        "cronjob_cancel": dict(room_denied=True),
        "cronjob_edit": dict(room_denied=True),
        "cronjob_schedule": dict(room_denied=True),
    },
    "dapp_browser": {
        # Disconnect REVOKES the injected wallet; it never grants anything.
        "dapp_browser_dapp_disconnect": dict(effect="none"),
        "dapp_browser_dapp_status": dict(effect="none"),
        # 042: bounded by a declared per-transaction ceiling AND a session budget
        # (spend_exemption checks both), every transaction simulated and asserted by tx_guard
        # inside the bridge.
        # ⚠️ NOT simulatable: ConnectParams has no dry_run field. Until 2026-09-23 an absent
        # dry_run meant simulate for every verb, so this connect never reached the owner queue
        # (security analysis H03a).
        # 042: connecting the wallet IS the authorization. The spend happens inside a browser
        # callback no hook can see, so the connect is the only place the owner can be asked.
        "dapp_browser_dapp_connect": dict(
            effect="money", lane="defi", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
    },
    "defi_data": {
        # room_denied: money-adjacent reads (the room toolset keeps defi_data for its impersonal
        # verbs).
        # 023 T1: own-holdings reads are the reconnaissance an attacker wants before a drain
        # (defi_data's impersonal verbs stay available).
        "defi_data_portfolio": dict(correspondent_blocked=True, room_denied=True),
        # 2026-09-15: the collectibles twin of the holdings read; a collection name is often
        # more identifying than a balance. defi_data_lp_positions: the same reconnaissance.
        "defi_data_nft_holdings": dict(correspondent_blocked=True, room_denied=True),
        "defi_data_lp_positions": dict(correspondent_blocked=True, room_denied=True),
        "defi_data_reconcile": dict(correspondent_blocked=True, room_denied=True),
        "defi_data_wallet_holdings": dict(correspondent_blocked=True, room_denied=True),
        "defi_data_wallet_activity": dict(correspondent_blocked=True, room_denied=True),
        # 071 W3: the rail book — sizes, cost basis and P&L of the OPERATOR's own positions.
        "defi_data_positions": dict(correspondent_blocked=True, room_denied=True),
    },
    "defi_trade": {
        # Every defi_trade verb signs a transaction: it moves value, pays gas, or grants/revokes
        # a standing authority. No verb is left to the ceiling, so EXTERNAL_WRITE_STRICT
        # classifies each one explicitly.
        # Enumerated by NAME although defi_trade is a high_impact tool: tool-id resolution
        # DEGRADES to the name-only path when the owner cannot be resolved, so the block must
        # not depend on a resolver succeeding.
        "defi_trade_swap": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_solana_swap": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # The Solana send (SOL or an SPL token). solana_swap's guard: simulated deltas asserted
        # to the lamport/raw unit, the outflow (incl. recipient-account rent) held to
        # max_spend_usd, PolicyGate, and the autonomous ceiling.
        "defi_trade_solana_transfer": dict(
            effect="money", lane="owner_always", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 023 T3/T4: every defi_trade verb is SPEND-side on the hook: irreversible and self-
        # custodial, never act-and-report in any mode. Loosening to the tiered lane is the
        # explicit owner flag DEFI_TIERED_SPEND_LANE, not a default.
        "defi_trade_transfer": dict(
            effect="money", lane="owner_always", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_approve_token": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_revoke_approval": dict(
            effect="money", lane="defi", side="spend", simulatable=True, risk_reducing=True,
            approval_owner="hook", correspondent_blocked=True,
        ),
        # 2026-09-12: the bridge JOINED the capped lane. The owner's verdict after using the
        # always-approve shape was that a money rail needing a tap per move is not autonomy.
        # Caps, not taps: the per-tx ceiling, the rolling daily cap and the simulated, asserted
        # deltas bound it, and the owner queue still catches anything over
        # DEFI_AUTONOMOUS_MAX_USD.
        # approval_owner=verb (039): the bridge asks the BETTER question itself (recipient, USD
        # value, arrival floor, Relay request id). Carrying both gates meant two taps for one
        # bridge, from two prompts describing it differently (2026-09-12). ONE gate, the verb's.
        # It stays on the capped lane.
        "defi_trade_bridge": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="verb",
            verb_gate="tools/defi/bridge_verb.py::_require_owner_approval",
            correspondent_blocked=True,
        ),
        # Wrapping native -> wrapped native: the destination is the chain registry's PINNED
        # wrapped_native, the rate is 1:1, and the value never leaves the wallet. tx_guard still
        # simulates it and asserts the native outflow.
        "defi_trade_wrap": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_unwrap": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_call": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_deploy_contract": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 042. Caps, not taps. A deployment's cost is its fee plus the native it endows, both
        # priced and held to max_spend_usd by tx_guard; call is held to a declared outflow AND a
        # declared MINIMUM INFLOW. Above the ceiling all three still reach the owner queue.
        "defi_trade_deploy_token": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 042b: the Solana twin, bounded by measured rent (~0.0035 SOL) held to max_spend_usd,
        # and by the same ceiling above it.
        "defi_trade_solana_deploy_token": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_lp_add": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # risk_reducing: an lp remove/collect and a revoke retire exposure; revoke_approval sets
        # an allowance to ZERO and carries no max_spend_usd, so it is tiered on identity.
        "defi_trade_lp_remove": dict(
            effect="money", lane="defi", side="spend", simulatable=True, risk_reducing=True,
            approval_owner="hook", correspondent_blocked=True,
        ),
        "defi_trade_lp_collect": dict(
            effect="money", lane="defi", side="spend", simulatable=True, risk_reducing=True,
            approval_owner="hook", correspondent_blocked=True,
        ),
        # owner_always, 2026-09-15: an NFT has NO reliable price (a floor is thin, wash-traded,
        # often absent), so tx_guard can only price the transaction at its worst-case fee, which
        # bounds nothing about what is sent. The OWNER is the bound: one env flag
        # (DEFI_TIERED_SPEND_LANE) must never wave through an asset of unknown value.
        "defi_trade_nft_transfer": dict(
            effect="money", lane="owner_always", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 2026-09-15: retiring a blanket operator approval on an NFT collection grants nothing
        # and moves nothing. Making the owner tap to REDUCE risk is how a wallet stays exposed
        # (risk_reducing).
        "defi_trade_nft_revoke_approval": dict(
            effect="money", lane="defi", side="spend", simulatable=True, risk_reducing=True,
            approval_owner="hook", correspondent_blocked=True,
        ),
        # 046: ERC-8004 identity. A registration's whole cost IS a bounded fee that tx_guard
        # prices and holds to max_spend_usd, so the capped lane is the right one; above it the
        # owner queue catches the act.
        # 046: sends nothing but creates a PERMANENT public identity from the treasury wallet;
        # not really a spend is the reasoning that once left solana_swap and x402_fetch
        # ungoverned.
        "defi_trade_register_agent": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "defi_trade_set_agent_uri": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
    },
    "agent_nft": {
        "agent_nft_inspect": dict(effect="none", correspondent_blocked=True),
        "agent_nft_snapshot": dict(effect="none", correspondent_blocked=True),
        # owner_always, 050: minting creates a durable identity asset from the treasury and take
        # moves the agent NFT to the owner principal; both are the owner's act by design
        # (050 §7.3 rule 4), so no flag may exempt them.
        "agent_nft_collection_mint": dict(
            effect="money", lane="owner_always", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
        "agent_nft_withdraw_token": dict(
            effect="money", lane="owner_always", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
        # C3: adopting writes the pfp, inherited positions and a handover entry (a fee-only
        # account call); the owner's act by design, like take.
        "agent_nft_adopt": dict(
            effect="money", lane="owner_always", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
        "agent_nft_bind_identity": dict(
            effect="money", lane="owner_always", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 050: fee-only writes. A journal anchor and a bind cost only the fee (held to
        # max_spend_usd); revoke_all only RETIRES standing approvals.
        "agent_nft_journal": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "agent_nft_revoke_all": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # reveal(ids) on a pinned collection costs only the fee (held to
        # AGENT_NFT_REVEAL_MAX_GAS_USD by tx_guard's is_collection_reveal shape, which also demands one
        # Revealed/Recommitted per id). Capped lane: the revealer runs every minute, unattended.
        "agent_nft_collection_reveal": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
    },
    "email": {
        # room_denied on every verb: outbound to anywhere but this room, or the
        # owner's mailbox (its contents and its organisation).
        "email_send": dict(effect="comms", room_denied=True),
        # The agent's own inbox (activation links / codes): a read, but of the agent's
        # private mailbox, so the agent_status disclosure class.
        "email_read_machine_mail": dict(effect="none", correspondent_blocked=True,
                                        room_denied=True),
        "email_reply": dict(effect="comms", room_denied=True),
        "email_forward": dict(effect="comms", room_denied=True),
        # Reads of the agent's own mailbox (read-only select, BODY.PEEK) and a
        # save into the session workspace: no external write.
        "email_list": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "email_read": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "email_folders": dict(effect="none", correspondent_blocked=True, room_denied=True),
        "email_save_attachment": dict(effect="none", correspondent_blocked=True, room_denied=True),
        # Mailbox organisation is classed comms on purpose: an owner pause on
        # comms also holds changes to the mailbox.
        "email_mark": dict(effect="comms", room_denied=True, correspondent_blocked=True),
        "email_move": dict(effect="comms", room_denied=True, correspondent_blocked=True),
        "email_delete": dict(effect="comms", room_denied=True, correspondent_blocked=True),
    },
    "git": {
        # Local repository work is not public; only a push reaches a public address.
        "git_add": dict(effect="none"),
        "git_branch": dict(effect="none"),
        "git_checkout": dict(effect="none"),
        "git_clone": dict(effect="none"),
        "git_commit": dict(effect="none"),
        "git_diff": dict(effect="none"),
        "git_log": dict(effect="none"),
        "git_pull": dict(effect="none"),
        "git_status": dict(effect="none"),
        # approval=recommended is the DEFAULT_APPROVAL_REQUIRED_TOOLS preset: NOT auto-applied
        # (an operator opts in with APPROVAL_REQUIRED_TOOLS + a non-auto provider); posture 2
        # unions it in. posture2 = the compute verbs gated at AGENT_COMPUTE_POSTURE >= 2.
        # always_queued = stays owner-queued even under AUTONOMY_MODE=autonomous.
        "git_push": dict(effect="public", approval=("recommended",), correspondent_blocked=True),
    },
    "github": {
        # git/github write verbs: covered by tool-id resolution too, named so a resolver fault
        # cannot open the ship-code path.
        "github_actions_logs": dict(effect="none"),
        "github_actions_runs": dict(effect="none"),
        "github_issue_list": dict(effect="none"),
        "github_pr_view": dict(effect="none"),
        "github_issue_create": dict(effect="public", correspondent_blocked=True),
        "github_merge_pr": dict(
            effect="public", approval=("recommended",), correspondent_blocked=True,
        ),
        "github_open_pr": dict(
            effect="public", approval=("recommended",), correspondent_blocked=True,
        ),
        "github_pr_comment": dict(effect="public", correspondent_blocked=True),
    },
    "goal": {
        # room_denied, deferred execution: a later owner-tenant run would execute what a
        # stranger planted in a room.
        "goal_create": dict(room_denied=True),
        "goal_ask": dict(room_denied=True),
        "goal_cancel": dict(room_denied=True),
    },
    "hf_deploy": {
        # hf_deploy_deploy is deliberately on NO approval lane: a blanket gate cannot tell a
        # FIRST publish (approved by the tool's own registry) from a redeploy of an approved app
        # (unattended within caps).
        # By NAME so a resolver fault cannot let a tainted session publish or delete a PUBLIC HF
        # Space.
        "hf_deploy_list_deployments": dict(effect="none"),
        "hf_deploy_deploy": dict(effect="public", correspondent_blocked=True),
        "hf_deploy_undeploy": dict(effect="public", correspondent_blocked=True),
    },
    "knowledge": {
        # M03: ingest/remove persist into what FUTURE sessions recall; search/list disclose what
        # the owner indexed. The knowledge tool row carries no high_impact on purpose: the gate
        # is by NAME.
        "knowledge_kb_remove": dict(correspondent_blocked=True),
        "knowledge_kb_search": dict(correspondent_blocked=True),
        "knowledge_kb_ingest": dict(correspondent_blocked=True),
        "knowledge_kb_list": dict(correspondent_blocked=True),
    },
    "launchpad": {
        # 042: named as well as by tool-id membership, so a resolver fault is not the only thing
        # in the way (dapp connect and the agent_nft verbs likewise).
        "launchpad_quote": dict(effect="none"),
        "launchpad_status": dict(effect="none"),
        "launchpad_buy": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        "launchpad_sell": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # 042: the launchpad writes. Caps, not taps: each is a bounded spend the guard simulates
        # and asserts; the owner queue still catches anything over DEFI_AUTONOMOUS_MAX_USD.
        "launchpad_launch": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
        # Caps, not taps: a claim is bounded by its FEE and the money moves TOWARD the treasury.
        # Making the agent ask to collect its own revenue is the shape the owner rejected on
        # 2026-09-12.
        "launchpad_claim": dict(
            effect="money", lane="defi", side="spend", simulatable=True, approval_owner="hook",
            correspondent_blocked=True,
        ),
    },
    "mcp": {
        "mcp_get_capabilities": dict(effect="none"),
        "mcp_get_server_status": dict(effect="none"),
        "mcp_health_check": dict(effect="none"),
        "mcp_list_resources": dict(effect="none"),
        "mcp_list_servers": dict(effect="none"),
        "mcp_list_tools": dict(effect="none"),
        "mcp_read_resource": dict(effect="none"),
        "mcp_execute_tool": dict(effect="network"),
    },
    "process": {
        # WS-2/3: by NAME (parity with shell_run) so a tool-id resolver fault cannot let a
        # tainted session kill or inspect the owner's background jobs.
        "process_list": dict(effect="none", correspondent_blocked=True),
        "process_log": dict(effect="none", correspondent_blocked=True),
        "process_poll": dict(effect="none", correspondent_blocked=True),
        "process_wait": dict(effect="none", correspondent_blocked=True),
        "process_kill": dict(effect="code", correspondent_blocked=True),
        # 073 W4: stdin to a running job (FIFO / PTY). A write can drive any program
        # the job runs, so it carries the same weight as the shell itself.
        "process_write": dict(effect="code", correspondent_blocked=True),
        "process_submit": dict(effect="code", correspondent_blocked=True),
        "process_close": dict(effect="code", correspondent_blocked=True),
    },
    "publish": {
        "publish_list": dict(effect="none"),
        "publish": dict(effect="public"),
        "publish_unpublish": dict(effect="public"),
    },
    "self_env": {
        # WS-5: owner-only via the posture gate already; a tainted session must never reach them
        # either.
        "self_env_read_source": dict(effect="none", correspondent_blocked=True),
        "self_env_git_pull": dict(
            effect="self", approval=("posture2", "always_queued"), correspondent_blocked=True,
        ),
        "self_env_install_dep": dict(
            effect="self", approval=("posture2", "always_queued"), correspondent_blocked=True,
        ),
        "self_env_patch_source": dict(
            effect="self", approval=("posture2", "always_queued"), correspondent_blocked=True,
        ),
        "self_env_restart_service": dict(
            effect="self", approval=("posture2", "always_queued"), correspondent_blocked=True,
        ),
    },
    "shell": {
        # 073 W2: `always_queued` — under AUTONOMY_MODE=autonomous a dangerous command
        # waits for the OWNER (owner_queue), never act-and-report. A safe command skips
        # the wait in every mode (tools/controller/command_guard_hook.py exemption).
        "shell_run": dict(
            effect="code", approval=("posture2", "always_queued"), correspondent_blocked=True,
        ),
    },
    "x402_invoice": {
        # P1-4: the request verb mints a payment request (the canonical forged-email target);
        # accounting/x402_invoices disclose the treasury ledger and payer contacts.
        # room_denied: money-adjacent (a room member must not mint a request or read the
        # ledger).
        "x402_invoice_accounting": dict(
            effect="none", correspondent_blocked=True, room_denied=True,
        ),
        "x402_invoice_x402_invoices": dict(
            effect="none", correspondent_blocked=True, room_denied=True,
        ),
        # RUNTIME name: the approval hook matches EXACTLY and container tools register as
        # {tool_id}_{action}. A bare x402_request matched nothing, so the lane never fired.
        # side=receive: the ONE act-and-report-eligible payment verb under
        # PAYMENT_APPROVAL_MODE=auto; every other hook verb is SPEND-side and keeps owner_queue
        # pre-approval in every mode (fail-safe: a new hook verb defaults to spend).
        "x402_invoice_x402_request": dict(
            effect="money", side="receive", approval=("recommended",), approval_owner="hook",
            correspondent_blocked=True, room_denied=True,
        ),
    },
    "x402_pay": {
        "x402_pay_x402_probe": dict(effect="none"),
        "x402_pay_x402_quote": dict(effect="none"),
        # x402_sweep is x402_probe over many URLs (tools/x402/discovery.py): it never signs, so
        # a spend pause must not refuse it.
        "x402_pay_x402_sweep": dict(effect="none"),
        "x402_pay_x402_wallet_status": dict(effect="none"),
        # The x402 auto-pay verb. On the payment hook (H1a) so an above-ceiling payment reaches
        # the owner queue, but a micro-payment inside X402_AUTONOMOUS_MAX_USD is waved through
        # by spend_lane._x402_exemption: blocking every $0.001 fetch on a tap makes x402
        # unusable.
        # H1a (audit 2026-08-22): the x402 auto-pay verb shipped on NO approval lane at all.
        # SPEND-side, so it keeps owner_queue pre-approval in every mode.
        # The auto-paying x402 action; the x402_pay_ prefix also catches it, but a row names the
        # thing it gates.
        "x402_pay_x402_fetch": dict(
            effect="money", lane="x402", side="spend", approval_owner="hook",
            correspondent_blocked=True,
        ),
    },
}

#: Names NO tool or controller action emits today, kept on purpose, each with its
#: policy so the decisions that name them do not change. Registered with tool=None.
#: A reserved name that starts being emitted must move to its tool's group above.
RESERVED_VERB_ROWS = {
    # Legacy tool_id tokens kept in the correspondent set so is_high_impact(tool_id) stays
    # truthy for callers that probe by tool id. Per-verb coverage comes from
    # HIGH_IMPACT_TOOL_IDS. Pack tool tokens (anysite, perplexity, twitter,
    # hyperliquid, polymarket — 067 P3/P4) stay here: the probe must answer the same
    # whether or not the pack is installed; the verb rows are in the pack.toml.
    "anysite": dict(correspondent_blocked=True),
    "browser": dict(correspondent_blocked=True),
    "code_execution": dict(correspondent_blocked=True),
    "coding": dict(correspondent_blocked=True),
    "cronjob": dict(correspondent_blocked=True),
    "email": dict(correspondent_blocked=True),
    "git": dict(correspondent_blocked=True),
    "github": dict(correspondent_blocked=True),
    "goal": dict(correspondent_blocked=True),
    "hf_deploy": dict(correspondent_blocked=True),
    "hyperliquid": dict(correspondent_blocked=True),
    "mcp": dict(correspondent_blocked=True),
    "perplexity": dict(correspondent_blocked=True),
    "polymarket": dict(correspondent_blocked=True),
    "process": dict(correspondent_blocked=True),
    "twitter": dict(correspondent_blocked=True),
    "web_fetch": dict(correspondent_blocked=True),
    "x402_invoice": dict(correspondent_blocked=True),
    "x402_pay": dict(correspondent_blocked=True),
    # Bare venue verbs: the anchors of the correspondent substring layer
    # (test_correspondent_trade_verbs_in_sync). The namespaced rows are the real coverage.
    "place_limit_order": dict(correspondent_blocked=True),
    "place_market_order": dict(correspondent_blocked=True),
    "cancel_order": dict(correspondent_blocked=True),
    "cancel_all_orders": dict(correspondent_blocked=True),
    "update_leverage": dict(correspondent_blocked=True),
    "approve_agent": dict(correspondent_blocked=True),
    "revoke_agent": dict(correspondent_blocked=True),
    # Aspirational self-evolution names: no action emits them today; a future action with the
    # name is gated from day one.
    "self_modify": dict(
        approval=("recommended", "always_queued"), correspondent_blocked=True, room_denied=True,
    ),
    "tool_manage": dict(approval=("recommended", "always_queued"), correspondent_blocked=True),
    "tool_manage_install": dict(room_denied=True),
    # Bare or legacy names a room turn is denied in advance: the runtime names are
    # knowledge_kb_* (also denied by the kb_ prefix rule) and email_send; defi_data_balances
    # does not exist today (defi_data_positions is a real verb since 071 W3, classed above).
    "kb_search": dict(room_denied=True),
    "kb_ingest": dict(room_denied=True),
    "kb_list": dict(room_denied=True),
    "kb_remove": dict(room_denied=True),
    "send_email": dict(room_denied=True),
    "defi_data_balances": dict(room_denied=True),
    # Special names tools/controller/approval_queue.py treats as money/individual-tap actions
    # (its literal moves with the P1b money kernel).
    "release_publish": dict(),
    "subscription_renewal": dict(),
}
