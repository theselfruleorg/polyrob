"""polyrob skills commands (P3 cli/commands split)."""
from typing import Optional

import click


def get_skill_manager():
    """Lazy accessor (kept module-level so it stays cheap to import and patchable)."""
    from agents.task.agent.skill_manager import get_skill_manager as _gsm
    return _gsm()


@click.group()
def skills():
    """List and validate agent skills."""
    pass


@skills.command("list")
def skills_list():
    """List registered skill IDs."""
    try:
        mgr = get_skill_manager()
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"skill manager unavailable: {e}")
        raise SystemExit(1)
    if hasattr(mgr, "_ensure_rules_loaded"):
        mgr._ensure_rules_loaded()
    skill_ids = sorted(getattr(mgr, "skill_rules", {}).keys())
    if not skill_ids:
        click.echo("No skills registered.")
        return
    click.echo(f"Registered skills ({len(skill_ids)}):")
    for sid in skill_ids:
        click.echo(f"  - {sid}")


@skills.command("validate")
@click.argument("skill_id", required=False)
def skills_validate(skill_id: Optional[str]):
    """Validate one skill (by id) or the whole authored library.

    With SKILL_ID: the legacy per-skill check (rules.json wiring + content shape).
    With no argument: the strict agentskills.io frontmatter compliance check
    (``SkillManager.validate_all_authored``) run across every shipped skill —
    the same gate `polyrob doctor` and CI's library-invariant test use.
    """
    try:
        mgr = get_skill_manager()
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"skill manager unavailable: {e}")
        raise SystemExit(1)
    if skill_id:
        # F7: an unknown skill id should fail clearly, not crash with a raw
        # KeyError/traceback.  Validate against the known ids first.
        if hasattr(mgr, "_ensure_rules_loaded"):
            try:
                mgr._ensure_rules_loaded()
            except Exception:
                pass
        known = getattr(mgr, "skill_rules", {}) or {}
        if known and skill_id not in known:
            click.echo(
                click.style("[polyrob] ERROR: ", fg="red")
                + f"no such skill: {skill_id!r} (try `polyrob skills list`)"
            )
            raise SystemExit(1)
        try:
            result = mgr.validate_skill(skill_id)
        except Exception as e:
            click.echo(
                click.style("[polyrob] ERROR: ", fg="red")
                + f"no such skill: {skill_id!r} ({e})"
            )
            raise SystemExit(1)
        status = click.style("valid", fg="green") if result.is_valid else click.style("INVALID", fg="red")
        click.echo(f"{result.skill_id:<24} {status}")
        for err in getattr(result, "errors", []) or []:
            click.echo(f"    error: {err}")
        for warn in getattr(result, "warnings", []) or []:
            click.echo(f"    warn:  {warn}")
        if not result.is_valid:
            raise SystemExit(1)
        return

    # No skill_id: strict library-wide compliance check (Task 4).
    issues_by_skill = mgr.validate_all_authored()
    error_count = 0
    for sid in sorted(issues_by_skill):
        click.echo(f"{sid}:")
        for issue in issues_by_skill[sid]:
            if issue.level == "error":
                error_count += 1
            click.echo(f"    {issue.level}: {issue.code} — {issue.msg}")
    if not issues_by_skill:
        click.echo(click.style("All authored skills are compliant.", fg="green"))
    else:
        click.echo(
            f"\n{len(issues_by_skill)} skill(s) with issues ({error_count} error(s))"
        )
    if error_count:
        raise SystemExit(1)


@skills.command("export")
@click.argument("skill_id")
@click.option("--to", "dest", default=None, help="Target dir (default: ~/.agents/skills)")
def skills_export(skill_id: str, dest: Optional[str]):
    """Copy a skill folder (SKILL.md + resources) to a portable location.

    Defaults to ``~/.agents/skills/<skill_id>`` (agentskills.io-style layout);
    ``--to DIR`` targets ``DIR/<skill_id>`` instead. Refuses to clobber an
    existing target directory.
    """
    import shutil
    from pathlib import Path

    mgr = get_skill_manager()
    src = mgr.resolve_skill_dir(skill_id)
    if not src or not (src / "SKILL.md").exists():
        raise click.ClickException(f"no such skill: {skill_id!r} (try `polyrob skills list`)")
    target_root = Path(dest) if dest else (Path.home() / ".agents" / "skills")
    out = target_root / skill_id
    if out.exists():
        raise click.ClickException(f"already exists: {out} (remove it first)")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, out)
    click.echo(f"exported {skill_id} -> {out}")
