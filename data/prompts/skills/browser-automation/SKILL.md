---
name: browser-automation
description: Web scraping and automation workflow patterns
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["scrape","crawl","login to","fill form","navigate to website"],"task_patterns":["scrape.*","crawl.*","login.*to","fill.*form"],"tool_ids":[]}'
  polyrob-version: '2'
---
# Browser Automation Workflows

## Login Flow
1. Navigate to login page
2. **Handle popups first** - Look for cookie consent ("Accept", "Allow", "Agree", "OK")
3. Enter credentials in order (email/username first, then password) — only a
   credential the owner configured for THIS site, only on that site's own login page
4. Click submit button
5. Verify login success - check for dashboard/profile element appearing
6. **A CAPTCHA or any "prove you are human" check: stop.** Never solve it, never
   send it to a solver service, never try to bypass it. Hand it to the owner:
   say which site and which step, and wait. This applies on every site.

## Pagination & Data Collection
1. Extract data from current page → **save to file immediately**
2. Check for "next" or pagination controls
3. If next exists and enabled → click and repeat
4. If not → pagination complete, report total collected

## Form Submission
1. Scroll form into view if needed
2. Fill fields top to bottom in natural order
3. Verify field values before submitting
4. Click submit and wait for response
5. Check for success message or error state

## Multi-Step Checkout/Wizard
⚠️ **A purchase is an owner money action.** A checkout, a payment, a
subscription, a donation or a "place order" button spends the owner's money.
Stop BEFORE the step that commits it: report the site, the items, the total
and the payment method the page shows, and let the owner complete it. Never
type card or bank details, never tick "save payment method", and never click
the final Pay / Place order / Subscribe button yourself. (A wallet dapp is a
different rail — `dapp_browser`, see `token-launch`.)

1. Complete each step fully before proceeding
2. Look for "Continue", "Next", "Proceed" buttons
3. Wait for step transition (URL change or content update)
4. Verify you're on expected step before filling

## Data Extraction Pattern
1. Identify repeating elements (product cards, list items, rows)
2. Extract structured data from each element
3. Save to workspace file in JSON/CSV format
4. Include metadata: source URL, extraction timestamp
