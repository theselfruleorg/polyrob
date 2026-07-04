---
name: browser-automation
description: Web scraping and automation workflow patterns
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["scrape","crawl","login to","fill form","navigate to website"],"task_patterns":["scrape.*","crawl.*","login.*to","fill.*form"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Browser Automation Workflows

## Login Flow
1. Navigate to login page
2. **Handle popups first** - Look for cookie consent ("Accept", "Allow", "Agree", "OK")
3. Enter credentials in order (email/username first, then password)
4. Click submit button
5. Verify login success - check for dashboard/profile element appearing

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
1. Complete each step fully before proceeding
2. Look for "Continue", "Next", "Proceed" buttons
3. Wait for step transition (URL change or content update)
4. Verify you're on expected step before filling

## Data Extraction Pattern
1. Identify repeating elements (product cards, list items, rows)
2. Extract structured data from each element
3. Save to workspace file in JSON/CSV format
4. Include metadata: source URL, extraction timestamp
