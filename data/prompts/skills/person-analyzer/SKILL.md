---
name: person-analyzer
description: Person intelligence workflow using LinkedIn and web research
license: MIT
metadata:
  polyrob-priority: '1'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["analyze person","research person","person intelligence","who is","profile analysis","sales prospect","contact research","background check","linkedin profile"],"task_patterns":["analyze.*person","research.*person","who is.*","find.*about.*person","profile.*analysis","prospect.*research","intelligence.*on"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Person Analyzer

Produce a strategic intelligence profile of an individual using LinkedIn, web sources, and public data.

## When to Use
Sales prospecting, partnership evaluation, investor research, talent assessment, contact research before meetings.

## Workflow

### Phase 1: Identify the person
Start with whatever identifiers you have (name, LinkedIn URL, company, role).

If `anysite` is available, discover what LinkedIn endpoints are available first, then query profile data. Do not hardcode endpoint paths — run a discovery call first to see what's available for the LinkedIn source, then pick the appropriate endpoint.

If `anysite` is unavailable or returns nothing, use this fallback order:
- `web_fetch` — read the person's LinkedIn or personal site URL if you have it
- `perplexity` — search for the person by name + company to find articles, bios, interviews
- `browser` — only for JS-rendered or login-gated pages (last resort)

### Phase 2: Gather activity data
Posts reveal priorities better than profile text. Collect at least 10–20 recent posts/comments if available.
- What topics they write about
- How they engage with others (comments, reactions)
- Tone: thought leadership, technical, promotional, personal

When using `anysite`: discover available endpoints for user posts, comments, and reactions for the relevant source (e.g., LinkedIn). Execute after discovery.

**URN note:** LinkedIn activity endpoints (posts, comments, reactions) typically require the person's URN in `urn:li:fsd_profile:ACoAA...` format — extract it from the profile response before querying activity.

### Phase 3: Company context
Understand the employer:
- Company industry, size, funding stage
- Recent news, leadership changes, product launches
- The company's social footprint

Use `anysite` (discover endpoints first) or `perplexity` / `web_fetch` for company data.

### Phase 4: External intelligence
Supplement with public sources:
- News articles, conference talks, podcast appearances
- GitHub, personal blog, academic papers (if technical role)
- Public community participation (Reddit, forums)

Use `web_fetch` for known URLs, `perplexity` for news synthesis.

### Phase 5: Synthesize and output

```markdown
# Person Intelligence Report: [Full Name]

**Generated:** [Date]
**Analysis Depth:** [Quick / Standard / Deep]
**Confidence Score:** [0–100%]

## Executive Summary
[2–3 sentences: who they are, why they matter for this task]

## Professional Profile
- **Current Role:** [Title] at [Company] (since [Date])
- **Location:** [City, Country]
- **Career Arc:** [Summary of trajectory]
- **Education:** [Degree, Institution]
- **Network Size:** [Connections count]

## Activity & Engagement Analysis
**Posting Frequency:** [X times per week / month]
**Primary Topics:**
1. [Topic] ([%] of posts)
2. [Topic] ([%] of posts)
3. [Topic] ([%] of posts)

**Engagement Style:** [how they interact publicly — thought leadership, technical, personal, etc.]
**Platform Activity:** [LinkedIn / Twitter / other — frequency, tone]

## Company Context
- **Employer:** [Name] — [Industry, ~Size, Stage]
- **Recent Signals:** [funding rounds, news, product changes]

## Connection Strategy
- **Best Channel:** [LinkedIn DM / warm intro / email / comment]
- **Conversation Starters:** [grounded in actual posts/topics they discuss]
- **Suggested Opening:** "[Specific line referencing their real content]"

## Priority Assessment
- **Tier:** [1 = hot / 2 = warm / 3 = nurture / 4 = low]
- **Rationale:** [why this tier]
- **Next Action:** [concrete step with timing]
```

## Analysis Depths

### Quick (1–5 min)
- Profile overview only
- 10–20 recent posts
- Basic assessment

### Standard (5–10 min) — DEFAULT
- Full profile details
- 20–50 posts analysis
- Company research
- Web intelligence
- Strategic recommendations

### Deep Dive (10–20 min)
- Extended post analysis (100+)
- Deep web research
- Comprehensive company intel
- Detailed connection strategy

## Notes
- State confidence and gaps explicitly — never fabricate details.
- If LinkedIn is paywalled or unavailable, note it and rely on web/perplexity.
- Cross-reference at least two sources for key claims.
- Always discover anysite endpoints before querying; never assume specific paths are available.
