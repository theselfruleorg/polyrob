# POLYROB Examples & Use Cases

Real-world examples of what you can build with POLYROB, organized by complexity and domain.

---

## Table of Contents

- [Quick Examples](#quick-examples)
- [Web Automation](#web-automation)
- [Data Extraction & Research](#data-extraction--research)
- [File Operations & Code](#file-operations--code)
- [Business Workflows](#business-workflows)
- [Scheduled Tasks](#scheduled-tasks)
- [Integrations](#integrations)
- [Advanced Workflows](#advanced-workflows)
- [Tips for Best Results](#tips-for-best-results)
- [Getting Help](#getting-help)

---

## Quick Examples

### One-Liners

```bash
# Summarize a webpage
polyrob run "summarize https://example.com/article"

# Screenshot a site
polyrob run "take a screenshot of https://github.com and save it"

# Quick research
polyrob run "search for the latest Python 3.12 release notes and summarize"

# File operations
polyrob run "create a markdown file with today's date as a todo list"
```

### Interactive Chat

```bash
polyrob chat

> You: Go to Product Hunt and find the top 3 AI tools launched today. Create a markdown file with descriptions and links.

> You: Monitor amazon.com for this product's price daily and alert me when it drops below $500.

> You: Analyze the code in ./src/ and find potential security issues.
```

---

## Web Automation

### Job Application Automation

**Task:** Fill out job application forms using resume data.

```bash
polyrob run "
Navigate to linkedin.com/jobs.
Search for 'Senior Python Engineer' roles in 'Berlin'.
For the top 5 matches:
  - Extract company name, job title, and requirements
  - Navigate to the company's application page
  - Fill out the form using my resume data from ~/resume.json
  - Save the application URL to job_applications.md
"
```

**What POLYROB does:**
1. Opens LinkedIn Jobs search
2. Parses job listings
3. Extracts key information
4. Navigates to each application
5. Fills forms (name, email, experience, etc.)
6. Logs progress and saves results

---

### Price Monitoring

**Task:** Monitor a product page and alert on price drops.

```bash
polyrob run "
Go to https://www.example.com/product/12345
Extract the current price from the page
If price < $500:
  - Send an email to me@example.com with subject 'Price Alert: Product is now $PRICE'
  - Take a screenshot of the page
Save the price history to price_history.json
"
```

**Made recurring:** ask the agent to schedule it as a cron job (durable across
restarts; needs `CRON_ENABLED=true` — see [CONFIGURATION.md](CONFIGURATION.md#tools--code-exec--cron--approvals)):

```bash
polyrob chat
> Schedule a recurring cron job: every day at 9am, check
> https://www.example.com/product/12345 and alert me if the price is under $500.
```

The agent calls its `cronjob_schedule` tool and confirms the job id and next run time.

---

### Account Reconciliation

**Task:** Log into multiple accounts and compile a report.

```bash
polyrob run "
Log into my bank at bank.example.com using credentials from ~/.bank_creds
Download the monthly statement for June 2026
Log into my credit card portal at cards.example.com
Download the monthly statement for June 2026
Compare transactions and create a reconciliation report:
  - Match transactions by date and amount
  - Flag discrepancies over $10
  - Save to reconciliation_june_2026.md
"
```

---

## Data Extraction & Research

### Competitor Analysis

**Task:** Research competitors and compile comparisons.

```bash
polyrob run "
Search for 'AI automation platforms 2026'
For the top 10 results:
  - Visit each company's website
  - Extract: pricing, key features, target customers
  - Note any unique differentiators
Create a comparison table in competitor_analysis.md with:
  - Company name
  - Pricing model
  - Key features
  - Differentiation
  - Strengths vs POLYROB
  - Weaknesses vs POLYROB
"
```

**Output example:** `competitor_analysis.md`
```markdown
# AI Automation Platforms Comparison (2026-06-28)

| Company | Pricing | Key Features | Differentiation | Strengths | Weaknesses |
|---------|---------|--------------|-----------------|----------|------------|
| POLYROB | Self-hosted, per-API cost | Multi-provider, durable goals | Provider failover | No vendor lock-in | Requires hosting |
| Competitor A | $99/month | Single-provider UI | Ease of use | Simple setup | Locked into one provider |
| Competitor B | Custom enterprise | Multi-agent workflows | Enterprise features | Expensive | Overkill for small teams |
...
```

---

### Market Research

**Task:** Aggregate market data from multiple sources.

```bash
polyrob run "
Research the 'AI agents for developers' market:
1. Search G2 and Capterra for products in this category
2. For each product found:
   - Extract pricing, user count, rating
   - Note key features and target users
3. Search Google for recent news (past 30 days)
4. Search Crunchbase for funding information
5. Compile a market report with:
   - Market size estimate
   - Key players and positioning
   - Pricing trends
   - Recent funding and M&A
   - Growth opportunities
Save as market_research_agents_2026.md
"
```

---

### Academic Research

**Task:** Gather papers and synthesize findings.

```bash
polyrob run "
Search arxiv.org for 'large language model agent orchestration' papers from 2025-2026
For the top 10 papers:
  - Download the PDF
  - Extract title, authors, abstract, key contributions
  - Summarize the methodology
  - Note citation count if available
Create a literature review with:
  - Thematic grouping of papers
  - Method comparison table
  - Gaps and future work
Save to lit_review_agent_orchestration_2026.md
"
```

---

## File Operations & Code

### Codebase Analysis

**Task:** Analyze a codebase and generate documentation.

```bash
polyrob run "
Analyze the code in ./src/:
1. List all Python files
2. For each file:
   - Extract function signatures and docstrings
   - Identify dependencies and imports
   - Note any TODO comments or FIXME markers
3. Generate API documentation:
   - Group functions by module
   - Include type signatures
   - Add usage examples
4. Create architecture.md with:
   - Module dependency graph
   - Data flow description
   - Key design patterns
Save all output to docs/generated/
"
```

---

### Security Audit

**Task:** Scan code for security issues.

```bash
polyrob run "
Perform a security audit of ./src/:
1. Search for common vulnerabilities:
   - SQL injection patterns
   - Hard-coded credentials (API keys, passwords)
   - Unsafe deserialization
   - Shell injection risks
2. Check dependency versions for known CVEs
3. Review authentication and authorization logic
4. Examine input validation and sanitization
Create security_report.md with:
  - Severity classification (Critical/High/Medium/Low)
  - Code location and line number
  - Description of the vulnerability
  - Recommended fix
  - Code example of safe implementation
"
```

---

### Test Generation

**Task:** Generate tests for existing code.

```bash
polyrob run "
For each module in ./src/:
1. Parse the module and extract:
   - All function signatures
   - Class definitions and methods
   - Exception handling
2. Generate pytest test cases:
   - Unit tests for each function
   - Edge cases and boundary conditions
   - Error handling scenarios
3. Create test files at tests/test_<module>.py
4. Ensure tests follow pytest fixtures and conventions
Run the generated tests and report coverage
"
```

---

## Business Workflows

### Invoice Processing

**Task:** Process invoices and update accounting records.

```bash
polyrob run "
Monitor emails@company.com for invoices:
1. For each new email with 'invoice' in subject:
   - Download the PDF attachment
   - Extract: vendor, invoice number, date, amount, line items
   - Validate against purchase order if exists
   - Classify by expense category
2. Update accounting_records.csv with new entries
3. For invoices over $5000:
   - Flag for manager approval
   - Send notification to accounting@company.com
4. Generate weekly summary report every Monday at 9am
"
```

**Made recurring:**
```bash
polyrob chat
> Schedule a recurring cron job: every Monday at 9am, process new invoices
> from emails@company.com, validate them, update accounting_records.csv,
> and send a weekly summary.
```

---

### Customer Onboarding

**Task:** Automate new customer setup.

```bash
polyrob run "
For each new customer signup from webhook:
1. Extract customer information
2. Perform setup tasks:
   - Create user account in database
   - Provision cloud resources (AWS/Azure)
   - Generate API keys
   - Send welcome email with getting-started guide
3. Create customer record in CRM
4. Schedule follow-up tasks:
   - Day 3: Check if customer has completed setup
   - Day 7: Send satisfaction survey
   - Day 30: Check usage and offer optimization tips
Log all actions to customer_onboarding.log
"
```

---

### Social Media Management

**Task:** Curate and post content.

```bash
polyrob run "
Daily content curation:
1. Search for industry news in 'AI automation'
2. Extract top 5 most shared articles
3. Generate:
   - Twitter thread summarizing key points
   - LinkedIn post with insights
   - Internal Slack digest for team
4. Schedule posts for optimal times:
   - Twitter: 9am, 2pm, 7pm EST
   - LinkedIn: 8am, 5pm EST
5. Track engagement metrics
6. Weekly report: top performing content, follower growth
"
```

---

## Scheduled Tasks

Recurring work is handled by the agent's cron tool, not a special CLI flag: ask for
it in natural language and the agent calls `cronjob_schedule` itself, which
persists to a durable job store and survives restarts. Requires `CRON_ENABLED=true`
(off by default — see [CONFIGURATION.md](CONFIGURATION.md#tools--code-exec--cron--approvals)).
`/cron` in the REPL lists scheduled jobs (read-only); ask the agent to cancel one
and it uses its `cronjob_cancel` tool.

### Daily Reports

**Task:** Generate and deliver daily summaries.

```bash
polyrob chat
> Schedule a recurring cron job: every weekday at 8am, check
> security@company.com for new alerts, scan cloud logs for anomalies, check
> dependency CVE feeds, and email a summary to security-team@company.com if
> anything critical is found.
```

### Weekly Maintenance

**Task:** Perform regular system maintenance.

```bash
polyrob chat
> Schedule a recurring cron job: every Sunday at 3am, check disk space,
> review error logs for patterns, test backup integrity, check SSL
> certificate expiry, and generate a maintenance report with any action
> items.
```

### Monthly Audits

**Task:** Monthly compliance and audit reports.

```bash
polyrob chat
> Schedule a recurring cron job: on the 1st of every month, review access
> logs for unauthorized access, check data retention compliance, verify
> encryption status for sensitive data, and generate a compliance report.
```

---

## Integrations

### Slack Bot Integration

**Task:** POLYROB as a Slack bot.

```python
# In your Slack bot app. Uses the OpenAI-compatible /v1 endpoint — a synchronous
# request/reply surface, enabled with OPENAI_COMPAT_API_ENABLED=true (see
# docs/guide/api.md). Session-based /api/task/sessions is fire-and-forget and
# not a fit for a bot that needs an immediate reply.
import requests

POLYROB_API = "http://localhost:9000"

def handle_slack_message(event):
    """Route a Slack message to POLYROB and get a synchronous reply."""
    user_message = event["text"]
    user_id = event["user"]

    response = requests.post(
        f"{POLYROB_API}/v1/chat/completions",
        json={
            "model": "gpt-5",
            "messages": [{"role": "user", "content": user_message}],
            "user": user_id,
        },
        headers={"X-API-KEY": POLYROB_API_KEY},
    )
    return response.json()["choices"][0]["message"]["content"]
```

**Example Slack interactions:**
```
User: @polyrob summarize the Jira board for Project X
POLYROB: [Analyzes Jira, returns summary]

User: @polyrob create a sprint report for last 2 weeks
POLYROB: [Generates and posts report]

User: @polyrob who worked on ticket PROJ-123?
POLYROB: [Looks up ticket, assigns and reports]
```

---

### Email Integration

**Task:** Let the agent send and receive email as a correspondent.

v1 email is Gmail-only (IMAP + SMTP via an app password, polled every
`EMAIL_IMAP_POLL_SEC` seconds — default 60) and **correspondent-only**: a reply
routes back as data into the session that first reached out, never as a command
a stranger can send. See [CONFIGURATION.md](CONFIGURATION.md) for the full flag list.

```bash
EMAIL_SURFACE_ENABLED=true
GMAIL_EMAIL=bot@gmail.com
GMAIL_APP_PASSWORD=your-16-char-app-password

polyrob email   # starts the IMAP-poll + SMTP surface in the foreground
```

With the surface running, give the agent a task that emails someone (it uses its
`email` tool) and any reply from that address routes back into the same session:

```bash
polyrob run "Email finance@company.com asking for this month's invoice totals"
```

---

### Webhook Integration

**Task:** Respond to webhooks from other services.

```python
# webhook_handler.py
from fastapi import FastAPI, Request
import requests

app = FastAPI()
POLYROB_API = "http://localhost:9000"

@app.post("/webhook/github")
async def github_webhook(request: Request):
    """Kick off a POLYROB session to analyze a newly opened PR."""
    payload = await request.json()

    if payload["action"] == "opened":
        pr_url = payload["pull_request"]["url"]
        response = requests.post(
            f"{POLYROB_API}/api/task/sessions",
            json={
                "task": f"Analyze this PR: {pr_url} and post a summary as a comment on it.",
                "user_id": "github_bot",
            },
            headers={"X-API-KEY": POLYROB_API_KEY},
        )
        # Session creation is fire-and-forget — it returns a session_id immediately,
        # not the analysis. Poll GET /api/task/sessions/{session_id} (or
        # `polyrob session show <id>`) for progress. Posting the PR comment itself
        # needs the optional `github` tool (GITHUB_TOOL_ENABLED=true + GITHUB_TOKEN);
        # PR comments are high-impact and approval-gated.
        return {"status": "started", "session_id": response.json()["session_id"]}
```

---

### GitHub Actions Integration

**Task:** Use POLYROB in CI/CD pipelines.

```yaml
# .github/workflows/polyrob-analysis.yml
name: POLYROB Code Analysis

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run POLYROB analysis
        env:
          GITHUB_TOOL_ENABLED: "true"   # opt-in: lets the agent post the PR comment itself
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          polyrob run "
          Analyze the changes in PR #${{ github.event.pull_request.number }} of
          ${{ github.repository }}:
          - Review the diff for security issues
          - Check for breaking changes
          - Validate API compatibility
          - Suggest improvements
          Post your findings as a comment on the PR.
          " --tools github,filesystem
```

---

## Advanced Workflows

### Multi-Agent Research

**Task:** Coordinate multiple specialized agents.

```bash
polyrob chat
> Delegate research on 'quantum computing in AI' to 3 parallel sub-agents:
>   - Agent 1: Focus on hardware advances
>   - Agent 2: Focus on algorithms and software
>   - Agent 3: Focus on commercial applications
>
> Synthesize findings into a comprehensive report with:
>   - Executive summary
>   - Technical deep-dive
>   - Market analysis
>   - Investment recommendations
```

---

### Continuous Learning

**Task:** Let the agent turn what it learns into reusable skills, on its own.

`POLYROB_LOCAL=true` already turns on `SKILLS_WRITABLE` and `BACKGROUND_REVIEW_ENABLED`
by default, so no manual scheduling is needed — a background reviewer forks off
every `BG_REVIEW_INTERVAL` productive turns (default 10) and can author or patch a
skill from what worked, staged for your review (`/skills` or `polyrob skill list`).

```bash
polyrob chat
```

If you'd rather have it run a deliberate nightly pass instead of (or in addition to)
the automatic reviewer, ask for a cron job:

```bash
> Schedule a recurring cron job: every day at 11pm, review today's conversations,
> extract patterns that worked, and update or create skills for them.
```

---

### Cross-Platform Coordination

**Task:** Coordinate work across multiple platforms.

```bash
polyrob run "
Start a project launch:
1. Create GitHub repository with template
2. Set up Jira board with sprint template
3. Create Slack channels for team
4. Send calendar invites for kick-off
5. Generate project wiki in Confluence
6. Post announcement to LinkedIn
7. Monitor all channels for responses
8. Compile feedback into project plan
"
```

---

## Tips for Best Results

### Be Specific

❌ **Vague:**
```
"Research AI companies"
```

✅ **Specific:**
```
"Search for 'AI automation platform' companies founded after 2020 with funding over $10M.
For each, extract: company name, funding amount, key investors, product focus.
Create a markdown report with a comparison table."
```

### Break Down Complex Tasks

❌ **Too complex:**
```
"Build a complete e-commerce site with payment processing"
```

✅ **Broken down:**
```
"Phase 1: Create a product catalog markdown file with 10 sample products.
Phase 2: Design the checkout flow as a flowchart.
Phase 3: Research Stripe payment integration.
Phase 4: Create a technical specification document."
```

### Use Goals and Cron for Ongoing Work

```bash
# One-time task — runs now, done when it's done
polyrob run "Check system logs and report errors"

# Durable, cross-session goal — survives restarts, no schedule attached
polyrob goals create "Log monitor" -b "Check logs for errors, alert if critical issues found" -p 7

# Truly recurring — ask the agent to schedule a cron job (needs CRON_ENABLED=true)
polyrob chat
> Schedule a recurring cron job: every day at 6am, check logs for errors and
> alert me if anything critical is found.
```

### Leverage Memory

```bash
polyrob chat
> Remember: Our API pricing is $0.01 per request for the first 10k, then $0.005
>
> New task: Generate a pricing calculator based on the pricing I just told you
```

---

## Getting Help

- **Documentation:** See [docs/guide/](guide/)
- **Configuration:** See [docs/CONFIGURATION.md](CONFIGURATION.md)
- **Architecture:** See [docs/guide/architecture.md](guide/architecture.md)
- **Community:** [GitHub Discussions](https://github.com/theselfruleorg/polyrob/discussions)
- **Issues:** [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)

---

## Contributing Examples

Have an interesting use case? We'd love to add it!

1. Fork the repository
2. Add your example to `docs/examples.md`
3. Submit a pull request with description

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.
