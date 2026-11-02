---
name: skill-authoring
description: How to write a durable, safe skill (read-before-edit, no injection, no secrets).
license: MIT
metadata:
  polyrob-when: 'Writing or editing a skill'
  polyrob-priority: '4'
  polyrob-version: '3'
---
# Skill Authoring

How to write a durable, safe skill in this system.

## Before you write
- Search the skill index before you create: when a skill already covers the procedure,
  load it and patch it instead of writing a second one.
- Read the existing skill first if you are EDITING one. Patch the smallest span; do not
  rewrite a whole body from memory — a stale rewrite can lose hard-won detail.
- A rule the owner stated is not a procedure: record it with `owner_doc_manage`, not in a
  skill. Only the terms of a programme the owner runs as jobs live in its pinned skill.
- An overwrite of an existing ACTIVE skill becomes a `.pending` proposal that the owner
  must promote. Do not expect an edit to take effect immediately; surface the pending id.

## Shape
- Start with a single `# Title` heading. Keep the body under 20,000 characters
  (`MAX_SKILL_INJECT_CHARS` — a bigger body still loads but bloats every prompt
  it joins; move depth into `references/` files). The hard limits: at least 50
  characters, at most 40,000 (`MAX_SKILL_FILE_CHARS`) — a bigger file is refused.
- Describe the procedure as advisory steps, tool-graceful: if a tool is absent, say so and
  fall back, never hard-fail.
- Give the skill a `metadata.polyrob-when` line in its frontmatter: when to load it, task
  first, at most 60 characters (e.g. `polyrob-when: 'Calibrating a widget'`). Sessions see
  only this line in their skill index; a longer line is refused.
- `tool_ids` in a skill are a GATE for the index only — they do NOT grant capabilities.
  Never imply a skill can use a tool the session lacks.

## Don't
- No "ignore previous instructions", no system-prompt reveal requests, no role-reset — the
  threat scanner rejects these (the skill body AND its description are scanned).
- No secrets/keys/tokens in the body (see secret-handling).
