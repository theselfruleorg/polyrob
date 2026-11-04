---
name: browser-automation
description: Interact with a website in the browser - log in, fill forms, page through results, stop before a purchase
license: MIT
metadata:
  polyrob-when: 'Driving a website: log in, click, fill forms'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["browser"]}'
  polyrob-version: '3'
---
# Browser Automation Workflows

This skill is about INTERACTION. To read a page you already have the URL of, use
`web_fetch_fetch_url` first (fast, no browser); to extract structured data, see
`web-research` or the `web-scraping` skill of the discovery pack.

## Tool availability
Read the `browser` line of your `<tool-catalog>`. `[loadable]` → `load_tool("browser")`.
`[gated:…]` → follow the remedy it names (see `harness-manual`); do not claim a page
you could not open.

## How the verbs work
- `browser_go_to_url(URL=…)` opens a page; the next observation lists the page's
  interactive elements with an index.
- `browser_click_element(elementIndex=N)` and
  `browser_input_text(elementIndex=N, inputText=…)` act on that index. Indices change
  after every navigation: read the new observation before the next click.
- `browser_send_keys(keys="Enter")`, `browser_scroll_down` / `browser_scroll_up`,
  `browser_scroll_to_text(text=…)`.
- Dropdowns: `browser_get_dropdown_options(elementIndex=N)`, then
  `browser_select_dropdown_option(elementIndex=N, inputText="<option text>")`.
- Tabs: `browser_open_tab(url=…)`, `browser_switch_tab(pageId=N)`.
- Read the page: `browser_extract_page_content()`.
- `browser_screenshot(name=…)` saves a PNG into the workspace (`screenshots/`) and
  returns its path. To show it, put that ABSOLUTE path in your `send_message` text (the
  framework attaches it or links it). Use `message(media_paths=[…])` only to send it to
  another chat.

## Login Flow
1. Navigate to the login page.
2. **Handle popups first** — cookie consent ("Accept", "Allow", "Agree", "OK").
3. Credentials: there is no site-password store. Use a login only when the owner
   gives it for this task, and only on that site's own login page. Type it into the
   field, never into a message, a file or memory. If the owner pasted it in chat, say
   once that it now sits in the chat history (`secret-handling`).
4. Click the submit button.
5. Verify the login: check that a dashboard or profile element appears.
6. **A CAPTCHA or any "prove you are human" check: stop.** Never solve it, never
   send it to a solver service, never try to bypass it. Hand it to the owner:
   say which site and which step, and wait. This applies on every site.
7. A login lasts for this session only: the next session starts logged out. Do the
   task in this session, and say so when the owner expects the login to last.

## Pagination & Data Collection
1. Extract data from the current page → **save to a workspace file immediately**.
2. Check for "next" or pagination controls.
3. If next exists and is enabled → click and repeat.
4. If not → pagination is complete; report the total collected.

## Form Submission
1. Scroll the form into view if needed.
2. Fill fields top to bottom in natural order.
3. Verify field values before you submit.
4. Click submit and wait for the response.
5. Check for a success message or an error state.
6. A file-upload field: there is no file-upload verb. Hand that step to the owner.

## Multi-Step Checkout/Wizard
⚠️ **A purchase is an owner money action.** A checkout, a payment, a
subscription, a donation or a "place order" button spends the owner's money.
Stop BEFORE the step that commits it: report the site, the items, the total
and the payment method the page shows, and let the owner complete it. Never
type card or bank details, never tick "save payment method", and never click
the final Pay / Place order / Subscribe button yourself. (A wallet dapp is a
different rail — `dapp_browser`, see `token-launch`.)

1. Complete each step fully before you proceed.
2. Look for "Continue", "Next", "Proceed" buttons.
3. Wait for the step transition (URL change or content update).
4. Verify you are on the expected step before you fill it.

## Results and files
- Save extracted data to a workspace file (JSON/CSV) with its source URL and time.
- A download started by a click is not saved to the workspace here. If you need the
  file, ask the owner for it or for a direct link.
- Deliver a result file or a screenshot: put its ABSOLUTE workspace path in your
  `send_message` text. Use `message(media_paths=[…])` only to send it to another chat.
