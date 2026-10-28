---
name: harness-manual
description: A tool you need is missing or refuses - read the true reason, say who fixes it and when, offer the one-tap fix.
license: MIT
metadata:
  polyrob-when: 'A tool you need is missing, gated or refuses'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-version: '1'
  polyrob-generated: 'devtools/gen_harness_manual.py from core/tool_gates.py REMEDY_ACTORS + core/verbs.py; do not edit'
---
# Harness manual

- Never name a cause you did not read from a tool result.
- Never advise a restart unless the remedy says restart.
- An operator fix is never a card: `propose_setting` files it; say "this needs
  the operator; I filed it as #N".

## Gate — who · when · fix
- loadable: agent · now · load_tool("<id>")
- money: owner-verb · new-session · /goal grant <id> tools=<tool>
- leaf-blocked: agent · now · report to the parent agent
- autonomous-ceiling: owner-verb · new-session · /goal grant <id> tools=<tool>
- unavailable-on-this-deploy: operator-env · restart · the named config
- custody-no-browser: operator-env · restart · the remote browser rail
- credentials-rejected: operator-env · restart · the rejected credentials
- unknown-tool: agent · now · a valid id from <tool-catalog>
- no-mcp-server: owner-verb · next-turn · /mcp add <id> <https-url>
- backend-unavailable: operator-env · restart · CODE_EXEC_BACKEND=<backend>
