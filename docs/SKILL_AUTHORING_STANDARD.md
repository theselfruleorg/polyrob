# Skill Authoring Standard

Rules for writing durable, safe system skills in this codebase. All shipped `SKILL.md` files must follow these rules. The invariant test `tests/unit/agents/task/test_library_invariants.py` enforces a subset mechanically.

---

## 1. Shape

- **Start with a single `# Title` heading.** The heading is the skill's human-readable name.
- **Keep the body under 12,000 characters.** Skills are injected into the system prompt; large bodies waste context and degrade agent focus. Aim for the minimum effective instruction set.
- **Write in the second person, active voice.** "Use `anysite` to query LinkedIn profiles" not "The agent should use anysite to query LinkedIn profiles."

## 2. Advisory + tool-graceful

Every step that requires a specific tool must have a fallback:

```
If `perplexity` is available, use it for synthesis. If not, fall back to `anysite` or `web_fetch`.
```

- **Never hard-fail on a missing tool.** Say "if X is unavailable, do Y instead."
- **Never imply a tool can do something the session doesn't have access to.** The `tool_ids` field in a rule is METADATA for matching — it does NOT grant capabilities. If the tool isn't loaded for the session, the skill still activates but the tool won't be callable.
- **State uncertainty.** If a data source might be paywalled or unavailable, say so and tell the agent to note it in the output.

## 3. `tool_ids` are matching metadata, not capability grants

The `tool_ids` array in `rules.json` under `triggers` tells the skill manager that this skill is relevant when those tools are loaded. It does **not** make those tools available.

```json
"triggers": {
    "tool_ids": ["anysite", "web_fetch"],
    ...
}
```

Every entry in `tool_ids` must be a member of `VALID_TOOL_IDS` (defined in `agents/task/agent/skill_manager.py`). Unknown tool IDs cause validation warnings.

## 4. anysite: always discover-first, never hardcode paths

The `anysite` tool has 200+ sources and 1,200+ endpoints. Endpoint paths change; hardcoding them makes skills brittle and misleading.

**DO:**
```
Use anysite_api to discover what endpoints are available for the LinkedIn source, then query the appropriate one.
```

**DON'T:**
```
anysite_api(endpoint='/api/linkedin/get_profile', params={"user": "..."})
# ↑ hardcoded path that may not exist or may have changed
```

**Rule:** Skill bodies must not hardcode `/api/...` paths. Teach the agent to discover endpoints first, then execute.

**Rule:** Skill bodies must never use the retired `mcp_execute_tool` verb. The current shape is `anysite_api(endpoint=..., params={...})` after discovery.

## 5. No hardcoded absolute paths

- No `/home/user/...`, no `/opt/rob/...`, no `~/.anysite/schema.json`.
- All file operations must be relative to the workspace (use `write_file`, `read_file` with relative paths, or rely on the path manager).
- The schema file is NOT on disk in the workspace. Never instruct the agent to read it from disk.

## 6. No secrets

- No API keys, tokens, passwords, or any credential in the body.
- Reference secrets by environment variable NAME only (e.g., "ensure `ANYSITE_API_KEY` is set").
- See the `secret-handling` skill for the full policy.

## 7. Read-before-edit

When refining an **existing** skill, always read the current body before writing. Never overwrite from memory — a stale rewrite can lose hard-won detail and regression-tested behavior.

```
# Correct workflow for editing an existing skill:
# 1. skill_manage(action="read", skill_id="my-skill") — read the current body
# 2. Plan the minimal diff
# 3. skill_manage(action="create", ...) with the merged body
```

Overwriting an active skill creates a `.pending` proposal that the owner must promote. Surface the pending ID to the user.

## 8. Security checks

The threat scanner scans both the skill **body** and its **description** for injection attempts. The following are rejected at write time:

- "Ignore previous instructions" or equivalents
- System-prompt reveal requests
- Role-reset instructions ("You are now...")
- Invisible/zero-width unicode characters
- Over-broad capability claims

Write skills that describe a procedure, not one that tries to override the agent's operating context.

## 9. rules.json entry format

Each new skill needs a `rules.json` entry:

```json
"skill-id": {
    "triggers": {
        "tool_ids": [],         // subset of VALID_TOOL_IDS; empty = matches regardless of tools
        "keywords": ["..."],    // short phrases that trigger this skill
        "action_names": [],     // action names that trigger this skill (usually empty)
        "task_patterns": ["..."]  // regex patterns matched against the task string
    },
    "priority": 6,             // lower = higher priority; 1-5 reserved for core skills
    "auto_activate": true,     // must have a SKILL.md body or the rule is pruned at load
    "description": "..."       // one sentence, also scanned for injection
}
```

**After editing rules.json:** always re-read it first to avoid clobbering another session's concurrent additions. The file is shared across parallel sessions.

## 10. Invariant test

`tests/unit/agents/task/test_library_invariants.py` enforces:
- Every `auto_activate` rule has a `SKILL.md` body.
- No `SKILL.md` contains `mcp_execute_tool`.
- No `SKILL.md` contains an obvious hardcoded secret (`sk-…`, `AKIA…`, PEM private key).

Run after adding a new skill:
```bash
python -m pytest tests/unit/agents/task/test_library_invariants.py -v
```

## 11. Note: scanner-flagged base skills are not re-forkable through the API

The `skill-authoring` and `skill-security-review` base skills legitimately quote
injection phrases (e.g. "ignore previous instructions") as *negated* authoring
guidance. They ship fine as built-in system skills (loaded from disk, trusted), but
because every API/agent write path now runs the injection threat-scan (a deliberate
security choke-point), **forking or re-authoring these two via `POST /api/skills/{id}/fork`
or the `skill_manage` action will be rejected (HTTP 400)**. This is fail-safe, not a bug —
do not add a scanner bypass for them. Copy their content into a new, differently-worded
skill if you need a variant.
