---
name: skill-authoring
description: How to write a durable, safe skill (read-before-edit, no injection, no secrets).
license: MIT
metadata:
  polyrob-when: 'Writing or editing a skill'
  polyrob-priority: '4'
  polyrob-owner-only: 'true'
  polyrob-version: '4'
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
- A skill you create or change is live at once, a bundled skill's fix too; the prior body
  is archived and `rollback` restores it. A write from a session with a correspondent's
  message, a room or another tenant stays a `.pending` draft for the owner.

## The calls
- Create: `skill_manage(action="create", skill_id=…, content=…, when="<= 60 chars",
  reason="<one sentence>")`.
- Change: `load_skill(skill_id)` first, then `skill_manage(action="patch", skill_id=…,
  old_string=…, new_string=…, expected_sha256=<the sha256 load_skill gave>, reason=…)`.
  `edit` replaces the whole body with the same two fields. A stale sha256 is refused:
  read the skill again. `when` alone (with `expected_sha256`) changes only the index line.
- An owner turn must give `reason`: the owner decides from it.
- `delete` archives the skill; `rollback` restores an archived revision. A builtin or pack
  id can never be created or deleted under your name.

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
