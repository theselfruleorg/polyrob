---
name: project-analyzer
description: Company/project analysis workflow with social presence and sentiment
license: MIT
metadata:
  polyrob-priority: '1'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["analyze company","company analysis","analyze project","social presence","brand analysis","competitor analysis","sentiment analysis"],"task_patterns":["analyze.*company","analyze.*project","social.*presence","brand.*analysis","company.*research","competitor.*","sentiment.*analysis"],"tool_ids":[]}'
  polyrob-version: '1'
---
# Project Analyzer

Produce a comprehensive intelligence report on a company or project: social presence, brand perception, leadership activity, and community sentiment.

## When to Use
Company research, brand/social audit, partnership evaluation, investment due diligence, competitor analysis, market research.

## Workflow

### Phase 1: Company foundation
Gather core company data:
- Profile: founding year, HQ, employee count, funding, industry
- LinkedIn company page: posts, follower count, content themes
- Web search: recent news, press releases, acquisitions

If `anysite` is available, discover endpoints for company sources (LinkedIn, Crunchbase, web search) before querying — do not assume endpoint paths. Fall back to `web_fetch` for known URLs and `perplexity` for news synthesis. Use `browser` only for pages requiring interaction.

### Phase 2: Social presence audit
Map the company across platforms:
- **LinkedIn** — company page activity, engagement, content themes
- **Twitter/X** — follower count, posting cadence, content style, community mentions
- **Instagram** — visual brand, follower count, engagement rate (if relevant)
- **YouTube** — channel, product demos, thought leadership videos (if relevant)

Discover available anysite endpoints for each platform before querying. For data retrieval (reading posts, profiles, search results), prefer `anysite` over the native `twitter` tool. Use the native `twitter` tool only for WRITE actions (posting, engaging).

### Phase 3: Leadership footprint
Key people who represent the company publicly:
- Find founders, CEO, CTO, key execs and their social handles
- What do they post about? How often? What is their thought leadership focus?
- Note any personal brands that are larger than the company brand

Discover the relevant anysite endpoints for people search and profile lookups.

### Phase 4: Community sentiment
What people say about the company:
- Reddit discussions (search for company name, reviews, comparisons)
- HackerNews threads
- G2/Capterra/Trustpilot reviews (use `web_fetch` or `browser`)
- Twitter/X mentions and community conversations

### Phase 5: Synthesize

```markdown
# Project Analysis Report: [Company/Project Name]

**Generated:** [Date]
**Analysis Depth:** [Quick / Standard / Deep]

## Executive Summary
[3–4 sentences: what they do, social presence strength, key findings, recommendation]

## Company Profile
- **Founded:** [Year] | **HQ:** [Location] | **Employees:** [~Count]
- **Funding:** [Amount, Stage, Investors]
- **Industry:** [Primary category]
- **Website:** [URL]

## Social Presence Summary

| Platform | Handle | Followers | Activity | Engagement |
|----------|--------|-----------|----------|------------|
| LinkedIn | | | | |
| Twitter/X | | | | |
| Instagram | | | | |

## Platform Analysis

### LinkedIn
- **Company Page Followers:** [Count]
- **Posting Frequency:** [X/week]
- **Content Themes:** [What they post about]
- **Top Performing Posts:** [Examples with engagement numbers]
- **Engagement Pattern:** [Typical comments, shares]

### Twitter/X
- **Followers:** [Count]
- **Activity Level:** [Posts/week]
- **Content Style:** [Product updates, thought leadership, etc.]
- **Engagement:** [Typical likes/retweets]
- **Key Conversations:** [What people say to/about them]

### Instagram (if applicable)
- **Followers:** [Count]
- **Content Type:** [Photos, reels, stories]
- **Visual Brand:** [Aesthetic, consistency]
- **Engagement Rate:** [Comments, likes]

## Leadership Social Footprint

| Person | Role | LinkedIn | Twitter | Activity Level |
|--------|------|----------|---------|----------------|
| [Name] | CEO | Active | Active | Posts 2×/week |
| [Name] | CTO | Active | None | Posts 1×/month |

### Key Thought Leadership Themes
- [What the CEO/founders talk about publicly]
- [Notable posts or engagement patterns]
- [Any personal brand that outshines the company brand]

## Community Sentiment
**Overall:** [Positive / Mixed / Negative]

**Positive themes:**
- [Theme] (X mentions)

**Complaints / concerns:**
- [Issue] (X mentions)

**Key quotes:**
> "[Actual quote from Reddit, HN, or review site]"
> "[Another quote]"

**Brand perception:**
- **Strengths:** [What people praise]
- **Concerns:** [What people complain about]
- **Competitive mentions:** [vs. competitors]

## Content Strategy Insights
- **Top Themes:** [What they focus on, % of content]
- **Best-performing content type:** [Format that gets engagement]
- **Posting cadence:** [How often per platform]
- **Posting times:** [Peak days/times based on post data]
- **Hashtag strategy:** [Tags they use consistently]

## Opportunities / Gaps
[Where their presence is weak or inconsistent]

## Recommended Actions
[3 specific actions with rationale]
```

## Analysis Depths

### Quick Assessment (5–10 min)
- Company profile only
- 10 recent posts per platform
- Basic sentiment check

### Standard Analysis (30–45 min) — DEFAULT
- Full company research
- All social platforms
- Leadership overview
- Reddit sentiment (30 posts)
- Content strategy analysis

### Deep Dive (60–90 min)
- Extended post analysis (100+ per platform)
- Deep Reddit mining
- Comprehensive leadership profiles
- Historical content analysis
- Full competitor comparison

## Notes
- Start with LinkedIn for reliable company data.
- Reddit often reveals unfiltered customer opinions — valuable for competitive positioning.
- State what you couldn't find and why (paywalled, no profile, low activity).
- Save intermediate findings to workspace files for large analyses.
- Always discover anysite endpoints before querying; never assume specific paths are available.
