---
name: skill-authoring
description: How to write a durable, safe skill (read-before-edit, no injection, no secrets).
license: MIT
metadata:
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"keywords":["author skill","write skill","create skill","edit skill"]}'
  polyrob-version: '2'
---
# Skill Authoring

How to write a durable, safe skill in this system.

## Before you write
- Read the existing skill first if you are EDITING one. Patch the smallest span; do not
  rewrite a whole body from memory — a stale rewrite can lose hard-won detail.
- An overwrite of an existing ACTIVE skill becomes a `.pending` proposal that the owner
  must promote. Do not expect an edit to take effect immediately; surface the pending id.

## Shape
- Start with a single `# Title` heading. Keep the body under 20,000 characters
  (`MAX_SKILL_INJECT_CHARS` — a bigger body still loads but bloats every prompt
  it joins; move depth into `references/` files). The hard limits: at least 50
  characters, at most 40,000 (`MAX_SKILL_FILE_CHARS`) — a bigger file is refused.
- Describe the procedure as advisory steps, tool-graceful: if a tool is absent, say so and
  fall back, never hard-fail.
- `tool_ids` in a skill are METADATA for matching only — they do NOT grant capabilities.
  Never imply a skill can use a tool the session lacks.

## Don't
- No "ignore previous instructions", no system-prompt reveal requests, no role-reset — the
  threat scanner rejects these (the skill body AND its description are scanned).
- No secrets/keys/tokens in the body (see secret-handling).
