# Skills & learning (reference)

Depth for the "Skills & learning" section of `polyrob-user-guide/SKILL.md`.

## What a skill is

A folder with a `SKILL.md` (agentskills.io-compliant Markdown + YAML
frontmatter) plus optional `references/`, `assets/`, `scripts/` resources —
the same open format Claude Code and other agents use. When a task matches a
skill's triggers, or you pick it from the catalog, you pull the full body
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

Three kinds of location, higher precedence wins on a name collision
(**project > user > builtin**; a builtin is never shadowed):

| Scope | Location | Writable? |
|---|---|---|
| builtin | the installed package (`data/prompts/skills/`) | read-only, trusted |
| user | per-tenant data home (`<data_dir>/skills/user_<uid>/`) | yes — install or agent-authored |
| external (discovered) | `~/.agents/skills/`, `~/.claude/skills/` (and per-repo `./.agents/skills/` on a trusted local CLI only) | no — loaded in place, not threat-scanned |

User/installed skills live under the data home, not the package tree, so they
survive `polyrob update`.

## Adding a skill — two paths

1. **Discovery** — drop a compliant folder into a discovery root and it's
   picked up on the next session, zero install step, but lenient-loaded (only
   `description` required) and — for project scope — local-operator-only
   (`POLYROB_TRUST_PROJECT_SKILLS`; a server never scans its own working
   directory for skills).
2. **Install** — `polyrob skill install <spec>` (local folder /
   `owner/repo/subdir` / git URL / direct `SKILL.md` URL) threat-scans every
   file, lands it in `.pending/` quarantine, and requires an explicit
   `polyrob skill approve <name>` before it's active. Remote sources are
   never auto-approved even with `--trust local`. This is the managed,
   audited path — prefer it for anything from outside the owner's own trust
   boundary.

## Owner-facing commands

```
polyrob skills list|validate|export           # authoring surface
polyrob skill install|approve|list|info|remove  # install pipeline
```
REPL equivalents: `/skills`, `/skills list|info <id>|install <spec>|
approve <id>|remove <id>`.

## `/learn` — teach a procedure conversationally

The owner can describe a procedure in prose with `/learn <description>`
(REPL). It's distilled deterministically (no model call) into a SKILL.md body
and always lands as a PENDING skill — the owner promotes it with
`/pending approve skill <id>` (or `/pending promote skill <id>`) once they've
reviewed it. This never auto-activates regardless of other writable-skill
settings — a described procedure is always owner-reviewed first.

## Writable skills (you authoring your own)

When `SKILLS_WRITABLE` is on (default off; on under `POLYROB_LOCAL`), you can
create/patch/delete/promote skills via the `skill_manage` action. Safety:

- Tenant-confined (`user_<uid>/` only), anonymous users blocked.
- Every write is threat-scanned (`is_suspicious`) — fails CLOSED on a scan
  error.
- `SKILLS_WRITABLE_REQUIRE_REVIEW` (default ON): your writes land in
  `.pending/` for owner review unless promoted; `SKILL_OVERWRITE_PROTECT`
  (default ON) means even overwriting an ACTIVE skill you already own becomes
  a pending proposal, never a silent clobber.
- **A forged/background/sub-agent/leaf turn (self-wake, async-delegation
  result, delegated worker, autonomous goal/cron run) can NEVER auto-activate
  a skill and NEVER patch/delete an active one** — it always quarantines,
  regardless of the review flag.
- Active authored skills need real keyword **triggers** to actually be
  match-eligible — a triggers-less write is a dead write.

## Read-before-edit

When refining an existing skill: `load_skill(skill_id=...)` to read the
current body first, plan the minimal diff, then `skill_manage(action="patch",
...)` — never blind-overwrite from memory of what a skill "probably" says.
