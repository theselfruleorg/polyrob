# Skill Authoring Standard

Rules for writing durable, safe system skills in this codebase. All shipped `SKILL.md` files must follow these rules. The invariant test `tests/unit/agents/task/test_library_invariants.py` enforces a subset mechanically.

---

## 1. Shape

- **Start with a single `# Title` heading.** The heading is the skill's human-readable name.
- **Keep the body focused (hard cap 40,000 chars, warning above 20,000).** Skills are pinned as a `SKILL`-origin foundation message (not the system prompt). With progressive disclosure ON (default), the skills that MATCHED the session (trigger matches, rail/persona seeds, `requires` prerequisites) are pinned in full while their bodies fit a 20,000-char budget (`SkillManager.EAGER_INJECT_BUDGET_CHARS`), prerequisites first. A matched skill that does not fit is listed `LOAD FIRST` in the `<skill-catalog>`, and every other skill is a catalog line the agent pulls with `load_skill`. A body over 20,000 chars is therefore never pinned; keep a skill that must be delivered under that size.
- **Write in the second person, active voice.** "Use `anysite` to query LinkedIn profiles" not "The agent should use anysite to query LinkedIn profiles."

## 2. Advisory + tool-graceful

Every step that requires a specific tool must have a fallback:

```
If `perplexity` is available, use it for synthesis. If not, fall back to `anysite` or `web_fetch`.
```

- **Never hard-fail on a missing tool.** Say "if X is unavailable, do Y instead."
- **Never imply a tool can do something the session doesn't have access to.** The `tool_ids` field in a rule is METADATA for matching — it does NOT grant capabilities. If the tool isn't loaded for the session, the skill still activates but the tool won't be callable.
- **State uncertainty.** If a data source might be paywalled or unavailable, say so and tell the agent to note it in the output.

## 2a. Capability-denial claims must be re-checkable, never asserted as permanent fact

Section 2 covers a tool being *unloaded for this session* — that's transient and self-correcting (the agent notices and falls back). A different, worse failure mode is a skill asserting that the underlying **capability itself doesn't exist** — "there is no Solana signer," "you cannot buy NFTs," "this tool cannot post" — as flat, permanent fact. That claim was true the day it was written and silently false the day the capability shipped, and nothing re-checks it: nothing calls a `SKILL.md`, so a stale denial doesn't error, it just makes the agent look like it's choosing not to act. This happened for real (2026-08-27): two prompts said Solana had no signer for weeks after the Solana rail shipped, and the agent repeated the false claim to its own owner every run.

- **If you write "X cannot do Y" or "there is no Z," add a matching row to `tests/unit/agents/task/test_capability_claims_registry.py` in the same commit.** The registry re-checks the claim against the actual code (a method that doesn't exist, a tool excluded from a frozenset, a flag that's off) on every test run, so the moment the capability ships, CI fails at that row instead of the claim rotting silently.
- **Prefer computing the fact at render time over hand-typing it**, wherever the rendering code already has the real answer — e.g. `agents/task/agent/prompts.py::_get_web_access_content` renders per-session tool availability from `self.tool_ids` rather than asserting a fixed list. A skill can't do this (it's static Markdown), which is exactly why it needs the registry instead.
- **Word the claim so it's falsifiable by a specific check**, not vibes — "there is no marketplace integration (no Seaport, no Reservoir)" is checkable; "NFTs aren't really our thing" is not.

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
# 1. load_skill(skill_id="my-skill") — read the current body
#    (skill_manage has no "read" verb; its actions are create/patch/delete/promote)
# 2. Plan the minimal diff
# 3. skill_manage(action="patch", ...) or action="create" with the merged body
```

Overwriting an active skill creates a `.pending` proposal that the owner must promote. Surface the pending ID to the user.

## 8. Security checks

The threat scanner scans both the skill **body** and its **description** for injection attempts.

New writes and promotions are refused if the scanner is unavailable or fails.
Tenant IDs must pass the shared tenant validator. User-scope reads and mutations
use descriptor-relative, no-symlink file operations; unsafe legacy paths require
owner remediation, not automatic migration. File replacement is atomic, but the
body and `rules.json` are not yet a single revision/CAS transaction.

The following are rejected at write time:

- "Ignore previous instructions" or equivalents
- System-prompt reveal requests
- Role-reset instructions ("You are now...")
- Invisible/zero-width/bidi unicode characters (hidden-instruction smuggling)

Write skills that describe a procedure, not one that tries to override the agent's operating context.

## 9. rules.json entry format

Each new skill needs a `rules.json` entry:

```json
"skill-id": {
    "triggers": {
        "tool_ids": [],         // subset of VALID_TOOL_IDS; empty = matches regardless of tools
        "keywords": ["..."],    // short phrases that trigger this skill
        "action_names": [],     // see 9a — never an always-registered action
        "task_patterns": ["..."]  // regex patterns matched against the task string
    },
    "priority": 6,             // lower = higher priority; see 9c for the ranges
    "auto_activate": true,     // must have a SKILL.md body or the rule is pruned at load
    "description": "...",      // one sentence, also scanned for injection
    "requires": []             // optional prerequisite skill ids, see 9b
}
```

### 9a. How a rule matches

- **Relevance is required.** A skill loads when a `keyword` (word-boundary match) or a `task_pattern` (regex, case-insensitive) hits the task. Only the first 4,000 characters of the task are matched (`SkillManager.MAX_TRIGGER_TASK_CHARS`). A `tool_ids` match alone never loads a skill.
- **`action_names` alone loads a skill only when `tool_ids` is empty.** Never list an action that is registered in every session (`preferences`, `owner_doc_manage`, `agent_status`, `done`, ...): the skill then matches every session and takes a capped slot. A shipped rule with empty `tool_ids` must keep `action_names` empty (`tests/unit/skills/test_trigger_matching_fixes.py`).
- **Money gate.** A rule that declares a MONEY tool (`core.tool_capabilities.ids_with("money")`, e.g. `defi_trade`) in `tool_ids` matches only when the session holds a declared money tool or a declared read tool with no `high_impact` capability (e.g. `defi_data`). A declared action tool that moves no funds (`cronjob`) does not open it.
- **`max_skills` is 2.** Trigger matches compete for two slots by `priority` (then match count).

### 9b. `requires` — prerequisite procedures

```json
"treasury-trading": { ..., "requires": ["token-identity", "pre-trade-check"] }
```

- Every loaded skill (matched or seeded) pulls in its `requires` ids, beyond `max_skills`, at most 2 per session (`MAX_PREREQUISITES`).
- One pass, no recursion: a prerequisite's own `requires` are not expanded.
- A prerequisite whose effective rule says `auto_activate: false` is not pulled in, and it must pass the money gate for the session.
- A prerequisite that a SELECTED parent requires does not compete for a capped slot; it is appended instead.
- Under progressive disclosure, prerequisites are pinned first (see section 1).

### 9c. Priority range

| Priority | Who |
|---|---|
| 0-5 | Shipped builtin skills (lower = higher priority); a pack skill carries its own rule priority (5-7 today) |
| 6 | Agent- and user-authored skills (the writer's default) |
| 9 | Ecosystem skills from `~/.claude/skills`, `~/.agents/skills` or a trusted project root (catalog only; `EXTERNAL_SKILL_PRIORITY`) |

### 9d. Ids and precedence

- **A user skill can never take a builtin or pack id.** `skill_manage create`, `promote`, `polyrob skill install` and `POST /api/skills` all refuse one (`SkillManager.reserved_skill_ids()`). A legacy user rule under a system id may only disable that skill (`"auto_activate": false`); it never replaces its triggers, `requires`, priority or gate.
- **One body precedence:** builtin (`data/prompts/skills/`) > the tenant's user skill > pack skill > ecosystem skill. `load_skill`, `resolve_skill_dir` (the `references/` directory) and the catalog use the same order. Among ecosystem roots, project > user; an ecosystem skill never enters the index under a builtin id.

### 9e. `allowed-tools` is advisory

An agentskills.io `allowed-tools` frontmatter field is recorded and one advisory line is appended to the delivered body (`agents/task/agent/skill_allowed_tools.py`). It is NOT enforced: the session's tool set does not change when the skill loads, and the names are often another harness's tool names.

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
