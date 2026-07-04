---
name: presentation-creator
description: Create reveal.js presentations with multi-file structure
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["presentation","slides","slideshow","slideshows","slide deck","deck","pitch deck","powerpoint","keynote"],"task_patterns":["create.*presentation","make.*(slides|slide ?deck|slideshow)","build.*deck","presentation.*about","slides.*for"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Presentation Creator

Build a slide deck as a set of files in the workspace. This is a recommended
workflow — adapt it when the request or available tools differ.

## When to use
Creating a presentation, slideshow, slide deck, pitch deck, or "powerpoint/keynote"
from a topic, outline, or source document.

## Workflow
1. **Clarify the brief** (or make sensible defaults): audience, length (slide count),
   and format. Default to **reveal.js single-file HTML** when unspecified — it has no
   external dependencies and previews in any browser.
2. **Outline first.** Draft a title + 5–9 section headings and confirm (or assume)
   the structure before writing slide content. One idea per slide; ≤6 bullets/slide.
3. **Write the deck to the workspace.** For reveal.js, write `slides.html` with one
   `<section>` per slide (use the CDN reveal.js stylesheet/script so it's self-contained).
   Keep speaker detail in `aside class="notes"` blocks, not on the slide.
4. **`.pptx` requested?** `python-pptx` may not be installed. Prefer reveal.js HTML or a
   plain Markdown deck; only attempt `.pptx` if you can confirm the library is available.
   If you can't produce the requested format, say so and offer the closest one.
5. **Save and report** the file path(s); offer to iterate slide-by-slide.

## Notes
- Write file content **verbatim** (do not reflow or strip indentation in code/HTML).
- Keep each deck in its own workspace subfolder if it spans multiple files.
