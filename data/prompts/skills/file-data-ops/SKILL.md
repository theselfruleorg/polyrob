---
name: file-data-ops
description: Read, transform and organize workspace files and structured data safely
license: MIT
metadata:
  polyrob-when: 'Reading, converting or organizing local files and data'
  polyrob-priority: '3'
  polyrob-triggers: '{"tool_ids":["filesystem"]}'
  polyrob-version: '2'
---
# File & Data Ops

Read, transform, and organize workspace files and structured data safely. Recommended
workflow — adapt as needed. Uses the `filesystem` tool when your `<tool-catalog>` holds
it (a group room does not); without it, say that you cannot open files here.

## When to use
Reading or parsing files, working with CSV/JSON/JSONL/Markdown/text, reading a PDF or
Word file, extracting or converting data, or organizing files in the workspace. For a
computed answer from a dataset (totals, groupings, a chart), use `data-analysis`.

## Workflow
1. **Inspect before you change anything.** `filesystem_list_directory`, then
   `filesystem_read_file` with `limit` (lines) or `char_limit` (dense JSON) for a sample.
2. **Tabular data:** read the header and a few rows to learn the schema first.
3. **Large files:** do not read 100k+ tokens into context. Find the region with
   `coding_grep` when `coding` is loaded; else read in windows (`offset`/`limit`,
   `char_offset`/`char_limit`). A whole-file read over the cap returns the TAIL and says so.
4. **Documents:**
   - `.pdf` / `.docx` — `filesystem_read_file` returns the extracted text; page it with
     `offset`/`limit`. A scanned PDF has no text layer: the result says so.
   - `.xlsx` / `.xls` / `.doc` / `.pptx` — `filesystem_read_file` cannot decode them.
     Use a script through `code_execution` (`openpyxl` for a spreadsheet) when it is
     loaded; else say that you cannot read the file and ask for a CSV or PDF.
   - A file the owner will ask about again: `knowledge_kb_ingest(path=…)`, then
     `knowledge_kb_search`.
5. **JSON-lines:** add with `filesystem_jsonl_append`, remove with
   `filesystem_jsonl_remove`, check with `filesystem_jsonl_validate`. Never rewrite the
   file by hand or with a pretty-printed object.
6. **Do not clobber the source.** `filesystem_copy_file` it first, or write the result to
   a NEW file. Make transforms idempotent.
7. **Validate the output** after writing: row/field counts, the expected schema.
8. **Report and deliver.** Say what changed. Deliver the result file: put its ABSOLUTE
   workspace path in your `send_message` text (the framework attaches it or links it;
   the result says which). Use `message(media_paths=[…])` only to send it to another
   chat.

## Notes
- Write file content **verbatim** — never reflow or strip indentation (it corrupts
  code, YAML, and indentation-sensitive formats).
- A Markdown file from an earlier session is a record: see `document-writing` before
  you rewrite one.
