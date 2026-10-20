<!-- part: main -->
Tool paths: relative to the workspace root (NO 'workspace/' prefix)
- Good: 'report.md', 'data/output.json'
- Bad: 'workspace/report.md'

Large Content (over {per_result} chars in one result, or {per_turn} in one step):
- Auto-saved to a workspace file
- The result shows "[LARGE CONTENT STORED]" with the file name and a preview
- Read the rest with filesystem_read_file(file_path=...){wire}
<!-- part: wire -->

- A smaller result over ~{wire_chars} chars shows its head (and, for a command, its tail); the full text is saved and the cut names the filesystem_read_file(..., char_offset=N) call that reads the rest
