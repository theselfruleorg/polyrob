---
name: lead-research
description: Find and qualify sales leads using LinkedIn and public data sources, producing a prioritized contact list
license: MIT
metadata:
  polyrob-priority: '6'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["find leads","prospect list","lead research","find contacts","qualify leads","outbound list","build lead list","sales prospects"],"task_patterns":["find.*leads","prospect.*list","lead.*research","build.*contact.*list","qualify.*leads","outbound.*campaign","sales.*prospect"],"tool_ids":["anysite"]}'
  polyrob-version: '1'
---
# Lead Research

Find and qualify sales leads or contacts using public data sources, producing a prioritized contact list with research notes.

## When to Use
Building a prospect list, finding decision-makers at target companies, qualifying inbound leads, preparing for outbound campaigns.

## Workflow

### 1. Define the ideal target profile (ICP)
Before searching, clarify:
- Target role(s) and seniority (e.g., VP Engineering, Head of Product)
- Target company size / industry / geography
- Any additional signals (funding stage, tech stack, recent news)

If the requester hasn't specified these, ask or make your assumptions explicit in the output.

### 2. Source leads via anysite
If `anysite` is available, use it as the primary research tool:
- Discover what endpoints are available for LinkedIn people search and company search.
- Run searches using keywords that match the ICP (role, company, industry).
- For each promising result, fetch the public profile data using the appropriate endpoint (discovered first — do not assume paths).

If `anysite` is unavailable or returns nothing:
- Use `web_fetch` to read public LinkedIn search result pages or company team pages if you have the URL.
- Use `perplexity` to search for named individuals by role + company.
- Use `browser` as a last resort for pages requiring interaction.

### 3. Qualify each lead
For each candidate, assess:

| Criterion | Questions |
|-----------|-----------|
| **Fit** | Does their role, company, and context match the ICP? |
| **Signal** | Any recent trigger (new role, funding round, blog post, job listing) that makes now a good time? |
| **Reachability** | Is there a public LinkedIn URL, email, or mutual connection? |
| **Priority** | Tier 1 (ideal + signal) / Tier 2 (good fit, no urgency) / Tier 3 (long-term nurture) |

Cross-reference company data: a VP at a 10-person seed startup is very different from a VP at a 5,000-person enterprise.

### 4. Build the output

```markdown
# Lead Research: [Campaign / Target Description]

**Date:** [Today]
**ICP:** [Role] at [Industry / Size] companies in [Geography]
**Search approach:** [sources used, queries run]
**Total:** [N leads] — Tier 1: [N] | Tier 2: [N] | Tier 3: [N]

## Lead List

| Name | Title | Company | LinkedIn | Signal | Tier |
|------|-------|---------|----------|--------|------|
| ... | ... | ... | ... | ... | 1 |

## Tier 1 Profiles

### [Name] — [Title] at [Company]
- **Why now:** [specific trigger or signal]
- **Recommended channel:** [LinkedIn DM / email / warm intro]
- **Opening angle:** [specific, referencing their context or signal]

## Data Gaps
[Leads where key info was unavailable; what would close the gap]

## Recommended Next Steps
1. [Action for Tier 1 leads — who, what, when]
2. [Action for Tier 2 leads]
```

## Notes
- Use `anysite` for LinkedIn profile and search data. Discover endpoints before querying; never hardcode endpoint paths.
- Respect privacy: only use publicly available information. Do not attempt to bypass privacy settings or access gated content.
- If using `perplexity`, cross-check Tier 1 leads against a second source before committing to a priority.
- State confidence: if a key field (email, exact role) is uncertain, flag it rather than guessing.
