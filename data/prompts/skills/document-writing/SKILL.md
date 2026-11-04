---
name: document-writing
description: Draft and edit long-form documents, reports and summaries
license: MIT
metadata:
  polyrob-when: 'Writing or editing a long document, report or summary'
  polyrob-priority: '3'
  polyrob-version: '3'
---
# Document Writing

Draft and edit long-form documents — reports, summaries, memos, articles.
Recommended workflow; adapt to the request. Uses the `filesystem` tool.

## When to use
Writing, drafting, composing, translating or summarizing into a structured document.

## Workflow
1. **Confirm the brief** (or set sensible defaults): audience, length, tone, format.
2. **Outline first.** Draft the headings/structure and get (or assume) sign-off
   before writing prose.
3. **Draft section by section.** One idea per paragraph; lead with the point.
4. **Revise pass:** cut filler, verify every factual claim, tighten structure and
   transitions.
5. **Save** to the workspace as Markdown (`.md`) with `filesystem_write_file`; offer to
   convert the format if needed.
6. **Long documents:** write incrementally to disk (`filesystem_append_file`) rather
   than holding the whole document in context.
7. **Deliver the file**, not only its name: put its ABSOLUTE workspace path in your
   `send_message` text (the framework attaches it or links it; the result says which).
   Put a short summary in the same text. Use `message(media_paths=[…])` only to send it
   to another chat.

## Summarizing a long input
Read a long PDF, Word file or page in windows (`filesystem_read_file` with
`offset`/`limit`; `file-data-ops` has the routes per file type). Write notes per section
to a workspace file, then summarize the notes. Keep section or page references, quote
exact numbers and names, and say what you skipped.

## Translating
Translate the prose. Keep names, numbers, code, commands and URLs as they are.

## Editing a Markdown file from an earlier session
The filesystem treats a `.md` file without a `kind: instruction` front-matter as a
RECORD (dated history). It refuses a rewrite or a delete of a record that this session
did not write. Then:
- append to it (`filesystem_append_file`), or write a new version as a NEW file; or
- if it really is a living page, first write the same content with the front-matter
  `kind: instruction` on top (the refusal names the exact shape), then edit it.

## Notes
- If the content rests on research, keep claims sourced (pair with the
  `web-research` workflow).
- Match the requested register; don't pad to hit a length.
