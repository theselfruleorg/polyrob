---
name: market-research-brief
description: Produce a structured market brief with size, key players, trends, and strategic opportunities
license: MIT
metadata:
  polyrob-priority: '6'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["market research","market brief","market analysis","market size","competitive landscape","tam","market opportunity","industry analysis"],"task_patterns":["market.*research","market.*brief","market.*analysis","competitive.*landscape","tam.*analysis","industry.*analysis","market.*size"],"tool_ids":["web_fetch","perplexity","anysite"]}'
  polyrob-version: '1'
---
# Market Research Brief

Produce a concise, structured market brief covering market size, key players, trends, and strategic opportunities.

## When to Use
Evaluating a new market, preparing for a product launch, competitive intelligence, investor deck preparation, strategy planning.

## Before You Start
Clarify scope — a vague question produces a vague brief. Pin down:
- **Market:** What product/service category? (be specific)
- **Geography:** Global, regional, or specific country?
- **Time horizon:** Current state only, or 1-year / 5-year outlook?
- **Angle:** Size + growth, competition, customer segments, technology trends, or regulation?

If scope is unclear, make your assumptions explicit in the output.

## Workflow

### 1. Gather market data

Use tools in this order of preference — fall back gracefully if one is unavailable:

1. **perplexity** — best for synthesizing recent market reports, analyst quotes, and statistics. Use first if available.
2. **anysite** — discover and use endpoints for company databases (Crunchbase, YC, LinkedIn company search) for competitive landscape data. Always discover endpoints before querying; do not hardcode paths.
3. **web_fetch** — read specific market reports, analyst blog posts, Wikipedia overview pages, Statista free tier pages.
4. **browser** — only if a source requires JS rendering or is otherwise inaccessible via fetch.

**Data to gather:**
- Market size (TAM / SAM / SOM if available) and growth rate (CAGR)
- Key players: incumbents, fast-growing startups, niche specialists
- Customer segments and their primary pain points
- Technology trends reshaping the market
- Regulatory or compliance factors
- Recent notable events (M&A, large funding rounds, major product launches, new entrants)

### 2. Cross-check key figures
Market size estimates vary widely. Always:
- Cite the source and year for every figure (e.g., "$12B — Gartner 2024")
- Note the range when sources disagree (e.g., "$4B–$7B depending on scope definition")
- Flag estimates that are >3 years old or from vendor-funded reports (potential bias)

### 3. Synthesize into a brief

```markdown
# Market Research Brief: [Market Name]

**Date:** [Today] | **Scope:** [Geography, time horizon]
**Prepared for:** [Purpose / audience]

## Executive Summary
[3–5 sentences: market size, growth rate, top players, 1–2 key insights, strategic takeaway]

## Market Overview
- **Estimated Size (TAM):** [Figure] ([Source, Year])
- **Growth Rate:** [CAGR %] through [Year] ([Source])
- **Scope note:** [What's included / excluded in this estimate]

## Key Players

| Company | Type | Revenue / Stage | Positioning | Recent Signal |
|---------|------|-----------------|-------------|---------------|
| [Name] | Incumbent | $XB | [Angle] | [News] |
| [Name] | Startup | Series B | [Angle] | [Funding] |

## Customer Segments
1. **[Segment]** — [Size, key pain point, buying behavior, willingness to pay]
2. **[Segment]** — [...]

## Trends Shaping the Market
1. [Trend + evidence + implication]
2. [Trend + evidence + implication]
3. [Trend + evidence + implication]

## Regulatory / Compliance Landscape
[Key regulations, pending changes, geographic variation that affects market access]

## Strategic Opportunities
1. [Opportunity + rationale + who it favors]
2. [Opportunity + rationale + who it favors]

## Risks & Headwinds
1. [Risk + likelihood + mitigation]
2. [Risk + likelihood + mitigation]

## Sources
- [Source 1: title, URL, date]
- [Source 2: title, URL, date]
```

## Notes
- A brief should be 1–3 pages (or the equivalent in tokens), not a 50-page report. Summarize and link to sources for depth.
- Always state what you couldn't verify and why (paywalled data, no recent figures, conflicting estimates).
- Vendor-sponsored reports tend to overestimate market size — note this explicitly when citing them.
- If `perplexity` is unavailable, combine `anysite` (company discovery for competitive landscape) with `web_fetch` (reading public reports and Wikipedia).
