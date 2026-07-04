---
name: document-writing
description: Draft and edit long-form documents, reports and summaries
license: MIT
metadata:
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["write","draft","compose","report","summarize","outline","essay","memo","article"],"task_patterns":["write.*(report|memo|doc|article|summary)","draft.*","compose.*","summari[sz]e.*","outline.*for"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Document Writing

Draft and edit long-form documents — reports, summaries, memos, articles.
Recommended workflow; adapt to the request. Uses the `filesystem` tool.

## When to use
Writing, drafting, composing, or summarizing into a structured document.

## Workflow
1. **Confirm the brief** (or set sensible defaults): audience, length, tone, format.
2. **Outline first.** Draft the headings/structure and get (or assume) sign-off
   before writing prose.
3. **Draft section by section.** One idea per paragraph; lead with the point.
4. **Revise pass:** cut filler, verify every factual claim, tighten structure and
   transitions.
5. **Save** to the workspace as Markdown (`.md`); offer to convert format if needed.
6. **Long documents:** write incrementally to disk rather than holding the whole
   document in context.

## Notes
- If the content rests on research, keep claims sourced (pair with the
  `web-research` workflow).
- Match the requested register; don't pad to hit a length.
