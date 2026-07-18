"""h_skills.py — the ``/skills`` REPL slash-command handler.

Read-only listing (with no argument or a bare query) of the skills the agent can
load, PLUS (Task 22) a subcommand dispatch layer mirroring the `polyrob skill`
CLI group over the install pipeline (Tasks 19-21): ``list``/``install``/
``approve``/``remove``/``info``. All subcommand logic is delegated to the shared
library functions in ``cli.commands.skill_install`` — no duplicated logic between
the CLI and the REPL. ``install``/``approve`` are gated on the local operator
(``agents.task.constants.local_mode_enabled``); Task 23 hardens the library layer
itself, this is just the REPL-side UX gate.

Kept in its own module (the god-file split convention — new behavior gets its
own file, see ``handlers.py``'s header). Registration is wired by the REPL /
default-registry builder, not here.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

from cli.ui import candy
from cli.ui.commands.registry import CommandContext

# Upper bound on how many skills we pull from the catalog. The library is small
# (a couple dozen), so this is effectively "all of them" while still bounding a
# pathological/misconfigured skills dir.
_MAX_SKILLS = 200
# Description truncation width for the one-line-per-skill listing.
_DESC_WIDTH = 80

# Subcommand names that route to the install-pipeline dispatch below. Anything
# else in ctx.args[0] is treated as a catalog filter query (backward compat —
# `/skills <query>` predates this dispatch layer).
_SUBCOMMANDS = frozenset({"list", "install", "approve", "remove", "info"})


def _truncate(text: str, limit: int) -> str:
    """Flatten whitespace and truncate *text* to *limit* chars (ellipsis)."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


async def h_skills(ctx: CommandContext) -> None:
    """List available skills, or manage them via a subcommand.

    Usage:
      /skills                    — list every available (auto-activatable) skill
      /skills <query>            — filter to skills whose id/description matches <query>
      /skills list               — full scope/state inventory (builtin/user/external
                                    x active/pending/archived) — mirrors `polyrob skill list`
      /skills info <id>          — frontmatter + provenance/usage for one skill
      /skills remove <id>        — archive (never hard-delete) a user skill
      /skills install <spec> [--trust local|prompt] [--ref REF]
                                  — local operator only; installs from a local
                                    folder / git spec / direct SKILL.md url
      /skills approve <id>       — local operator only; activates a quarantined skill

    Read-only paths are fail-open: any backend error degrades to a friendly
    one-liner (never raises into the REPL).

    This handler is ``async`` (the dispatcher awaits it) so the blocking
    install/approve pipeline (git clone / URL fetch / filesystem promotion) can
    be pushed off the event-loop thread via ``asyncio.to_thread`` — otherwise a
    ``/skills install`` would freeze the whole REPL (no render, no input) for
    the duration of the clone. The cheap read paths (list/info/remove/catalog)
    stay synchronous calls inside this coroutine; they don't block on I/O.
    """
    args = ctx.args or []
    if args and args[0].strip().lower() in _SUBCOMMANDS:
        sub = args[0].strip().lower()
        rest = args[1:]
        if sub == "list":
            _cmd_list(ctx)
        elif sub == "info":
            _cmd_info(ctx, rest)
        elif sub == "remove":
            _cmd_remove(ctx, rest)
        elif sub == "install":
            await _cmd_install(ctx, rest)
        elif sub == "approve":
            await _cmd_approve(ctx, rest)
        return
    _cmd_catalog(ctx)


def _require_local_operator(ctx: CommandContext, action: str) -> bool:
    """Gate a mutating subcommand (install/approve) on the local operator.

    Task 23 will harden this at the library layer too; this is the REPL-side
    UX refusal so a non-local (multi-tenant/remote) session gets an honest
    message instead of silently installing/activating content."""
    try:
        from agents.task.constants import local_mode_enabled

        if local_mode_enabled():
            return True
    except Exception:
        pass
    ctx.emit(
        f"Refused: `{action}` requires the local operator (POLYROB_LOCAL) — "
        "not available from a remote/multi-tenant session.",
        title="skills",
    )
    return False


def _cmd_list(ctx: CommandContext) -> None:
    """`/skills list` — full scope/state inventory, mirrors `polyrob skill list`."""
    try:
        from cli.commands.skill_install import list_all_skills

        rows = list_all_skills(ctx.user_id or "local")
    except Exception as exc:
        ctx.emit(f"Could not list skills: {exc}", title="skills")
        return
    if not rows:
        ctx.emit(candy.empty("skills found", yet=False), title="skills")
        return
    lines = [f"{len(rows)} skill row(s):"]
    for row in rows:
        extra = ""
        if row.get("source"):
            extra += f" source={row['source']}"
        if row.get("sha"):
            extra += f" sha={row['sha'][:8]}"
        text = f"{row['id']:<24} {row['scope']:<14} {row['status']:<10}{extra}"
        lines.append(candy.status_line(row["status"], text))
    ctx.emit("\n".join(lines), title="skills")


def _cmd_info(ctx: CommandContext, rest: List[str]) -> None:
    """`/skills info <id>` — frontmatter + provenance/usage, mirrors `polyrob skill info`."""
    if not rest:
        ctx.emit("Usage: /skills info <id>", title="skills")
        return
    skill_id = rest[0]
    try:
        from cli.commands.skill_install import get_skill_info

        info = get_skill_info(skill_id, ctx.user_id or "local")
    except Exception as exc:
        ctx.emit(str(exc), title="skills")
        return
    ctx.emit(candy.kv_lines(list(info.items())), title=f"skill: {skill_id}")


def _cmd_remove(ctx: CommandContext, rest: List[str]) -> None:
    """`/skills remove <id>` — archive-never-delete, mirrors `polyrob skill remove`."""
    if not rest:
        ctx.emit("Usage: /skills remove <id>", title="skills")
        return
    skill_id = rest[0]
    try:
        from cli.commands.skill_install import remove_skill

        ok = remove_skill(skill_id, ctx.user_id or "local")
    except Exception as exc:
        ctx.emit(f"Remove failed: {exc}", title="skills")
        return
    if ok:
        ctx.emit(f"Removed skill {skill_id!r} (archived — recoverable).", title="skills")
    else:
        ctx.emit(f"Could not remove {skill_id!r} (not found or not permitted).", title="skills")


def _parse_install_flags(rest: List[str]):
    """Pull ``spec``/``--trust``/``--ref`` out of the raw REPL arg list."""
    if not rest:
        return None, "prompt", None
    spec = rest[0]
    trust = "prompt"
    ref = None
    i = 1
    while i < len(rest):
        tok = rest[i]
        if tok == "--trust" and i + 1 < len(rest):
            trust = rest[i + 1]
            i += 2
            continue
        if tok == "--ref" and i + 1 < len(rest):
            ref = rest[i + 1]
            i += 2
            continue
        i += 1
    return spec, trust, ref


async def _cmd_install(ctx: CommandContext, rest: List[str]) -> None:
    """`/skills install <spec>` — LOCAL OPERATOR ONLY. Mirrors `polyrob skill install`.

    ``dispatch_install`` does synchronous network/subprocess I/O (git clone /
    URL fetch), so it is run via ``asyncio.to_thread`` to keep it off the
    event-loop thread and avoid freezing the REPL during a clone.
    """
    if not _require_local_operator(ctx, "install"):
        return
    spec, trust, ref = _parse_install_flags(rest)
    if not spec:
        ctx.emit("Usage: /skills install <spec> [--trust local|prompt] [--ref REF]", title="skills")
        return
    try:
        from cli.commands.skill_install import dispatch_install

        res = await asyncio.to_thread(
            dispatch_install, spec, user_id=ctx.user_id or "local", trust=trust, ref=ref
        )
    except Exception as exc:
        ctx.emit(f"Install failed: {exc}", title="skills")
        return
    if res.approved:
        ctx.emit(f"Installed + approved skill {res.name!r} (active).", title="skills")
    else:
        ctx.emit(
            f"Installed skill {res.name!r} to quarantine — "
            f"/skills approve {res.name} to activate.",
            title="skills",
        )


async def _cmd_approve(ctx: CommandContext, rest: List[str]) -> None:
    """`/skills approve <id>` — LOCAL OPERATOR ONLY. Mirrors `polyrob skill approve`.

    ``_approve`` promotes a quarantined skill (filesystem promote + resource
    port + ``rmtree`` + content re-scan), so it is offloaded via
    ``asyncio.to_thread`` to keep that I/O off the event-loop thread.
    """
    if not _require_local_operator(ctx, "approve"):
        return
    if not rest:
        ctx.emit("Usage: /skills approve <id>", title="skills")
        return
    name = rest[0]
    try:
        from cli.commands.skill_install import _approve

        await asyncio.to_thread(
            _approve, name, user_id=ctx.user_id or "local", source="local"
        )
    except Exception as exc:
        ctx.emit(f"Approve failed: {exc}", title="skills")
        return
    ctx.emit(f"Approved skill {name!r} (active).", title="skills")


def _cmd_catalog(ctx: CommandContext) -> None:
    """Original (pre-Task-22) behavior: list/filter the auto-activatable catalog.

    Read-only. Fail-open: any backend error degrades to a friendly one-liner
    (never raises into the REPL).
    """
    try:
        from agents.task.agent.skill_manager import get_skill_manager

        manager = get_skill_manager()
        skills = manager.get_catalog_skills(
            user_id=ctx.user_id or "local", max_skills=_MAX_SKILLS
        )
    except Exception as exc:
        ctx.emit(f"Could not load skills: {exc}", title="skills")
        return

    if not skills:
        ctx.emit(candy.empty("skills available", yet=False), title="skills")
        return

    # Optional case-insensitive filter over id + description.
    query = (ctx.args[0].strip().lower() if ctx.args else "")
    if query:
        skills = [
            s
            for s in skills
            if query in str(getattr(s, "skill_id", "")).lower()
            or query in str(getattr(s, "description", "")).lower()
        ]
        if not skills:
            ctx.emit(candy.empty(f"skills match {ctx.args[0]!r}", yet=False), title="skills")
            return

    # Sort readably by id (the catalog is already priority-sorted; id-sort makes
    # the flat list scannable).
    skills = sorted(skills, key=lambda s: str(getattr(s, "skill_id", "")))

    lines: List[str] = [f"{len(skills)} skill(s) available:"]
    for s in skills:
        skill_id = str(getattr(s, "skill_id", "") or "?")
        desc = _truncate(getattr(s, "description", "") or "", _DESC_WIDTH)
        lines.append(candy.bullet(skill_id + (f" — {desc}" if desc else "")))
    lines.append("Use `load_skill(skill_id=\"<id>\")` (agent) to load a skill's full body.")

    # Task 10 (SK-F2 visibility): the catalog view above only ever shows
    # active/auto-activatable skills — a quarantined (.pending/) draft
    # authored by the agent or a background review is otherwise invisible.
    # Reuse the EXISTING scope/state inventory (`list_all_skills`, the same
    # helper `/skills list` uses) rather than hand-globbing `.pending/`.
    pending_count = _pending_count(ctx)
    if pending_count:
        lines.append(
            f"Pending review: {pending_count} — approve with `polyrob skill approve <id>` "
            "(or `/skills approve <id>`)."
        )
    ctx.emit("\n".join(lines), title="skills")


def _pending_count(ctx: CommandContext) -> int:
    """Count this tenant's quarantined (`.pending/`) skill drafts. Fail-open (0)."""
    try:
        from cli.commands.skill_install import list_all_skills

        rows = list_all_skills(ctx.user_id or "local")
        return sum(1 for row in rows if row.get("status") == "pending")
    except Exception:
        return 0
