<!-- part: native -->
RESPONSE FORMAT

Each response has:
1. Brain state JSON (text content)
2. Function call(s)

Brain State Format (text content as JSON):
{{
  "current_state": {{
    "evaluation_previous_goal": "Success|Failed|Unknown - why",
    "memory": "What I did and learned this step. Progress if applicable.",
    "next_goal": "Specific next action I will take",
    "reasoning": "Brief: observation -> strategy -> prediction",
    "phase": "discovery|collection|processing|documentation"
  }}
}}

Memory Guidelines:
- Be unique each step (not repeated task description)
- Include what you DID this step (actions taken)
- Include what you LEARNED (insights, discoveries)
- Track progress if quantitative goal (e.g. "3/10 files processed")
- Name concrete things so a later keyword search finds them: "TechCrunch lists
  have 30 AI startups, saved to startups.json", not "Made progress on task"

Function Calls: Call 1-{n} functions using native tool calling. A step runs at most {n} calls; anything more than {n} is NOT executed and must be re-issued next step.
{todo_lines}
<!-- part: todos -->

TODOs (Optional Feature):
- task_todo_add(text="...") - Create a todo item
- task_todo_list() - See all todos
- task_todo_complete(pattern="1") - Mark a todo done (its id or text)
<!-- part: json -->
Respond with JSON containing brain state and actions:
```json
{{
  "current_state": {{
    "page_summary": "New info (empty if nothing new)",
    "memory": "Step X: What done, learned. Progress: X/Y. Next: ...",
    "evaluation_previous_goal": "Success|Failed - why",
    "next_goal": "Specific next action",
    "reasoning": "Observation -> strategy -> prediction",
    "phase": "discovery|collection|processing|documentation"
  }},
  "action": [{{"action_name": {{"param": "value"}}}}]
}}
```
