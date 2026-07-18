"""`polyrob knowledge` — export the agent's knowledge as an Obsidian vault (C3).

Thin Click wrapper over ``core.knowledge_export.build_vault``. Bootstrap follows
``cli/commands/kb.py``: wire the active memory provider (with the embedder when
available) so notes/episodes come from the same memory.db the agent uses.
Export-only: the DBs stay the SSOT; the vault is a projection.
"""
from __future__ import annotations

import asyncio
import os

import click


def _bootstrap() -> None:
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    os.environ.setdefault("POLYROB_LOCAL", "1")


async def _resolve_provider_and_data_dir():
    """The active external memory provider + data dir.

    The data home comes from the bootstrap resolver DIRECTLY (it honors
    POLYROB_DATA_DIR — the headless case) rather than from the built container:
    a container-build failure (e.g. no LLM API key in the environment) must
    never silently redirect the export to the wrong dir. The container is only
    consulted for the optional embedder (vector recall); export is read-only
    and works FTS-only without it.
    """
    import logging as _logging
    embedder = None
    try:
        from core.bootstrap import _resolve_cli_data_home
        data_dir = str(_resolve_cli_data_home()[0])
    except Exception:
        from pathlib import Path
        data_dir = str(Path.cwd() / ".polyrob")
    try:
        from core.bootstrap import build_cli_container
        _logging.disable(_logging.CRITICAL)
        try:
            container = await build_cli_container(log_level="ERROR")
        finally:
            _logging.disable(_logging.NOTSET)
        if container.has_service("embedding_model"):
            embedder = container.get_service("embedding_model")
    except Exception as e:
        click.echo(click.style(f"note: container build skipped (FTS-only): {e}", dim=True))
    provider = None
    try:
        from modules.memory.backend_factory import maybe_register_memory_backend
        provider = maybe_register_memory_backend(data_dir=data_dir,
                                                 embedding_model=embedder)
    except Exception as e:
        click.echo(click.style(f"note: memory backend unavailable: {e}", dim=True))
    return provider, data_dir


@click.group("knowledge")
def knowledge():
    """Inspect and export what the agent knows."""


@knowledge.command("export")
@click.option("--out", "out_dir", default="./knowledge-vault", show_default=True,
              help="Vault output directory (opened directly in Obsidian).")
@click.option("--since", "since", default=None,
              help="Only episodes newer than this (8h / 2d / ISO date).")
@click.option("--user", "user", default="local", show_default=True,
              help="Tenant to export.")
def export(out_dir: str, since, user: str):
    """Write notes/episodes/skills/identity/goals as a markdown vault."""
    _bootstrap()

    async def go():
        provider, data_dir = await _resolve_provider_and_data_dir()
        since_ts = None
        if since:
            from modules.memory.episodic import parse_since
            since_ts = parse_since(since)
            if since_ts is None:
                click.echo(click.style(f"could not parse --since {since!r}", fg="red"))
                raise SystemExit(2)
        from core.knowledge_export import build_vault
        return await build_vault(out_dir, user_id=user, data_dir=data_dir,
                                 provider=provider, since_ts=since_ts)

    manifest = asyncio.run(go())
    click.echo(click.style(f"vault written to {out_dir}", fg="green"))
    for section, count in manifest.items():
        click.echo(f"  {section}: {count}")
    click.echo(click.style(
        "open the folder as an Obsidian vault — wikilinks and graph view work.",
        dim=True))


__all__ = ["knowledge"]
