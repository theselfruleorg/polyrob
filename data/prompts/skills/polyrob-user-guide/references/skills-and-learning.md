# Skills & learning (reference)

Depth for the "Skills & learning" section of `polyrob-user-guide/SKILL.md`.

## What a skill is

A folder with a `SKILL.md` (agentskills.io-compliant Markdown + YAML
frontmatter) plus optional `references/`, `assets/`, `scripts/` resources —
the same open format Claude Code and other agents use. A pinned skill loads when a
chat or job starts; for any other, you pick it from the catalog and pull the full body
with `load_skill(skill_id=...)` and follow it; resources under it are read
(never executed) with `read_skill_resource`.

## Progressive disclosure

By default you see a compact `<skill-catalog>` — id + one-line description
per skill (~20-30 tokens each), not full bodies. Call
`load_skill(skill_id="<id>")` to load a skill's FULL instructions BEFORE doing
the work it covers; load only what the current step needs. A repeated
`load_skill` for an already-active skill in the same session short-circuits
to a brief "already active" acknowledgment instead of re-emitting the body.

## Scopes and precedence

One precedence order (`core/skills/scopes.py`), higher wins on a name
collision: **builtin > user > pack > project > user-external**. A builtin or
pack id is protected — an external skill under it is skipped.

| Scope | Location | Writable? |
|---|---|---|
| builtin | the seeded copy `<data_home>/skills/bundled/` (from the package's `data/prompts/skills/`) | no — a fix is an owner-accepted change to the seeded copy |
| user | per-tenant data home (`<data_home>/skills/user_<uid>/`) | yes — install or agent-authored |
| pack | a loaded pack's skills directory | no |
| project / user-external | `./.agents/skills/`, `./.claude/skills/` (trusted local CLI only), then `~/.agents/skills/`, `~/.claude/skills/` | no — loaded in place; injection-scanned before it enters the catalog |

User/installed skills live under the data home, not the package tree, so they
survive `polyrob update`.

## Adding a skill — two paths

1. **Discovery** — drop a compliant folder into a discovery root and it's
   picked up on the next session, zero install step, but lenient-loaded (only
   `description` required) and — for project scope — local-operator-only
   (`POLYROB_TRUST_PROJECT_SKILLS`; a server never scans its own working
   directory for skills).
2. **Install** — `polyrob skills install <spec>` (local folder /
   `owner/repo/subdir` / git URL / direct `SKILL.md` URL) threat-scans every
   file, lands it in `.pending/` quarantine, and requires an explicit
   `polyrob skills approve <name>` before it's active. Remote sources are
   never auto-approved even with `--trust local`. This is the managed,
   audited path — prefer it for anything from outside the owner's own trust
   boundary.

## Owner-facing commands

```
polyrob skills list|validate|export           # authoring surface
polyrob skills install|approve|list|info|remove  # install pipeline
```
REPL equivalents: `/skills`, `/skills list|info <id>|install <spec>|
approve <id>|remove <id>`.

## `/learn` — teach a procedure conversationally

The owner can describe a procedure in prose with `/learn <description>`
(REPL). It's distilled deterministically (no model call) into a SKILL.md body
and is live at once.

## Writable skills (you authoring your own)

When the `skill_manage` action is loaded (its setting is a posture member of
the autonomy group — see `references/configuration.md`), you can
create/patch/edit/delete/rollback skills (`promote` is the owner's). Safety:

- Tenant-confined (`user_<uid>/` only), anonymous users blocked.
- Every write is threat-scanned (`is_suspicious`) — fails CLOSED on a scan
  error.
- Your writes are live at once, from a chat, a job, a goal or a reviewer, and a fix
  to a bundled skill too. Every overwrite archives the prior body, every change is in
  the ledger, and `rollback` restores the earlier text.
- **A session with a correspondent's message, a room turn or another tenant can
  NEVER activate a skill or change an active one**: the write stays a `.pending` draft
  for the owner (`/pending approve skill <id>`).
- An authored skill needs a `when` line (60 characters or fewer): it is the
  skill's one line in the catalog, and the only way the agent finds it.

## Read-before-edit

When refining an existing skill: `load_skill(skill_id=...)` to read the
current body and its sha256 first, plan the minimal diff, then
`skill_manage(action="patch", expected_sha256=<that sha256>, reason=…, ...)` — a
stale sha256 is refused; never blind-overwrite from memory of what a skill
"probably" says. The `skill-authoring` skill has every call.
