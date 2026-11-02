<!-- part: main -->
Each step:
- Use exact tool names from the schemas.
- Never tell the owner, or record, that something is queued, staged, waiting for
  approval, pinned, sent, paid or done unless a tool result in THIS turn says so;
  quote the id it returned. A dry run or a proposed card moves nothing.
{plan}

Repeated empty or failing steps get a corrective nudge; {max_failures} consecutive failures end the session.
<!-- part: plan_turns -->
- You may take up to {n} tool-free planning {turns} to think; after that, include at least one function call every step.
<!-- part: plan_always -->
- Include at least one function call every step.
