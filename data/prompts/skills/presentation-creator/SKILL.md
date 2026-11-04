---
name: presentation-creator
description: Create a reveal.js slide deck as one HTML file (or a .pptx when the sandbox can build one)
license: MIT
metadata:
  polyrob-when: 'Making a slide deck (reveal.js)'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["filesystem"]}'
  polyrob-version: '2'
---
# Presentation Creator

Build a slide deck in the workspace. This is a recommended workflow — adapt it when
the request or available tools differ.

## When to use
Creating a presentation, slideshow, slide deck, pitch deck, or "powerpoint/keynote"
from a topic, outline, or source document.

## Workflow
1. **Clarify the brief** (or make sensible defaults): audience, length (slide count),
   and format. Default to **one reveal.js HTML file** when unspecified. It loads
   reveal.js from its CDN, so it needs network to render; say so if the owner will
   present offline, and offer a Markdown deck instead.
2. **Outline first.** Draft a title + 5–9 section headings and confirm (or assume)
   the structure before writing slide content. One idea per slide; ≤6 bullets/slide.
3. **Write the deck to the workspace.** Write `slides.html` with one `<section>` per
   slide, linking the reveal.js stylesheet and script from the CDN. Keep speaker
   detail in `aside class="notes"` blocks, not on the slide.
4. **`.pptx` requested?** If `code_execution` is loaded, build it with
   `code_execution_run_code(language="python", packages=["python-pptx"], code=…)` and
   save the file into the workspace (`packages` needs the sandbox-dev posture and
   sandbox network; the result says when it is refused). Otherwise offer reveal.js
   HTML or a Markdown deck, and say why.
5. **Deliver the file**: put its ABSOLUTE workspace path in your `send_message` text
   (the framework attaches it or links it; the result says which). Use
   `message(media_paths=[…])` only to send it to another chat. Offer to iterate slide
   by slide.

## Notes
- Write file content **verbatim** (do not reflow or strip indentation in code/HTML).
- Keep each deck in its own workspace subfolder if it spans several files.
- To put the deck at a public URL, see `ship-a-web-app` (the `publish` verb).
