---
name: task-planning
description: Decompose a fuzzy goal into a verifiable plan and decide when to delegate
license: MIT
metadata:
  polyrob-priority: '4'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["make a plan","plan this","break down","how should i approach","roadmap","decompose","milestones","step-by-step plan"],"task_patterns":["\\b(make|write|draft|create) (a |an |the )?plan\\b","\\bplan (out|this|the|my|our)\\b","\\bbreak\\b.*\\bdown\\b.*\\b(task|goal|project|work)\\b","how (should|do) (i|we) approach","what (are the )?steps"],"tool_ids":[]}'
  polyrob-version: '2'
---
# Task Planning

Turn a fuzzy or multi-step goal into an ordered, verifiable plan before acting, and
decide what to delegate. Recommended workflow — adapt it.

## When to use
A vague or multi-step request ("plan", "break down", "how should I approach", "what
steps"), or any task big enough that jumping straight to action risks rework.

## Workflow
1. **Restate the goal** and define "done" — the concrete success criteria — before
   planning.
2. **Decompose** into ordered, verifiable steps. Track them with the `task` TODO tool
   so progress is visible.
3. **Find the independent work.** Steps with no shared state can run in parallel —
   use `delegate_task` (2–5 parallel sub-tasks) for those; keep dependent work
   sequential.
4. **Surface unknowns and risks up front** rather than discovering them mid-build.
5. **Checkpoint** after each step; re-plan when a step reveals new information.

## Notes
- Don't over-plan a small task — match plan depth to the work.
- Delegation has depth/role limits; a leaf/sub-agent turn cannot itself delegate.
