---
name: file-data-ops
description: Read, transform and organize local files and structured data safely
license: MIT
metadata:
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["read file","csv","json","parse","extract data","convert","organize files","transform data","spreadsheet"],"task_patterns":["(read|parse|convert|extract).*(csv|json|md|txt|file)","organize.*files","transform.*data"],"tool_ids":[]}'
  polyrob-version: '1'
---
# File & Data Ops

Read, transform, and organize local files and structured data safely. Recommended
workflow — adapt as needed. Uses the `filesystem` tool (always available).

## When to use
Reading/parsing files, working with CSV/JSON/Markdown/text, extracting or converting
data, or organizing files in the workspace.

## Workflow
1. **Inspect before mutating.** List the directory and read the target (or a sample)
   first; confirm the structure before changing anything.
2. **For tabular data**, read the header + a few sample rows to learn the schema
   before transforming.
3. **Don't clobber the source.** Make transforms idempotent and reversible — write
   results to a NEW file rather than overwriting the input.
4. **Validate the output** after writing: check row/field counts and that the schema
   is what you expected.
5. **Large files:** don't read 100k+ tokens into context — `grep`/scan to locate the
   relevant region, then read just that part (stream/chunk when transforming).
6. **Report** what changed and the output path.

## Notes
- Write file content **verbatim** — never reflow or strip indentation (it corrupts
  code, YAML, and indentation-sensitive formats).
