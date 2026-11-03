---
name: harness-manual
description: A tool you need is missing or refuses - read the true reason, say who fixes it and when, offer the one-tap fix.
license: MIT
metadata:
  polyrob-when: 'A tool you need is missing, gated or refuses'
  polyrob-priority: '2'
  polyrob-version: '1'
  polyrob-generated: 'devtools/gen_harness_manual.py from core/tool_gates.py REMEDY_ACTORS + REFUSAL_KINDS + core/verbs.py; do not edit'
---
# Harness manual

- Never name a cause you did not read from a tool result.
- Never advise a restart unless the remedy says restart.
- An operator fix is never a card: on an owner turn `propose_setting` files it
  (say "this needs the operator; I filed it as #N"); on a goal/cron run raise ONE
  `owner_ask`.

## Gate — who · when · fix
- loadable: agent · now · load_tool("<id>")
- money: owner-verb · new-session · /goal grant <id> tools=<tool>
- leaf-blocked: agent · now · report to the parent agent
- autonomous-ceiling: owner-verb · new-session · /goal grant <id> tools=<tool>
- unavailable-on-this-deploy: operator-env · restart · the named config
- not-constructed: deploy · deploy · a deploy shape that builds it
- custody-no-browser: operator-env · restart · the remote browser rail
- credentials-rejected: operator-env · restart · the rejected credentials
- unknown-tool: agent · now · a valid id from <tool-catalog>
- no-mcp-server: owner-verb · next-turn · /mcp add <id> <https-url>
- backend-unavailable: operator-env · restart · CODE_EXEC_BACKEND=<backend>
- pack-not-loaded: operator-env · restart · polyrob pack enable <pack>
- disabled-by-flag: operator-env · restart · polyrob config set <FLAG> true
- load-failed: deploy · deploy · a fix for the init error
- wallet_owner: owner-verb · now · the owner's own session
- owner_only: owner-verb · now · the owner's own session
- autonomous_wait: agent · now · continue; read the answer next run
- wallet-off: operator-env · restart · AGENT_WALLET_ENABLED=true
- tool-off: operator-env · restart · polyrob doctor
- seat-room: owner-verb · now · the owner's private chat
- seat-correspondent: owner-verb · now · the owner's own chat

## Refusal — who · when · lever · retry
- approval:denied_by_owner: owner-verb · now · none — final · no retry
- approval:pending: owner-verb · now · /pending <pending_id> · retry
- approval:timeout: owner-verb · now · /pending <pending_id> · retry
- approval:timeout_no_ask: owner-verb · now · the remedy it names · retry
- approval:crashed: operator-env · restart · the approval provider · retry
- approval:refused: owner-verb · now · the remedy it names · no retry
- approval_not_created:forged: owner-verb · now · the owner runs it from their chat · no retry
- approval_not_created:tainted: owner-verb · now · the owner runs it from their chat · no retry
- approval_not_created:store_down: agent · now · retry later · retry
- approval_not_created:no_provider: operator-env · restart · APPROVAL_PROVIDER · no retry
- approval_not_created:money_owner: owner-verb · now · the owner runs it from their chat · no retry
- approval_not_created:busy: owner-verb · now · the owner answers the open prompt · retry
- approval_not_created:duplicate: agent · now · none — final · no retry
- approval_not_created:closed: agent · now · retry to ask again · retry
- seat:room: owner-verb · now · the owner runs it from their private chat · no retry
- seat:correspondent: owner-verb · next-turn · the owner's own message (or they run it from their chat) · no retry
- money:forged_text: owner-verb · now · the owner runs it from their chat · no retry
- money:autonomous_unarmed: operator-env · restart · the money regime and DEFI_AUTONOMOUS_TURN_TRADING · no retry
- money:gate: owner-verb · now · the owner's caps or their one-shot approval · no retry
- denylist: owner-verb · next-turn · the owner's approvals.deny preference (/prefs) or POLYROB_TOOL_DENYLIST · no retry
- paused: owner-verb · now · /resume · no retry
- strict_code_change: deploy · deploy · a row in core/effects.py WRITE_ACTIONS or NON_WRITE_ACTIONS · no retry
- retry_wall: agent · next-turn · change the call, or the owner's next message · no retry
- unattended:goal: owner-verb · now · an owner turn · no retry
- unattended:cron: owner-verb · now · an owner turn · no retry
- unattended:run: owner-verb · now · an owner turn · no retry
