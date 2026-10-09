"""polyrob kb command group — CLI knowledge-base management.

Provides four subcommands (add / list / remove / search) that call the
Task-6 engine (``kb_ingest``) and the Task-5 registry routers
(``kb_search`` / ``kb_list_sources`` / ``kb_remove``) without
re-implementing any walking, chunking, or provider logic.

Bootstrap preamble mirrors ``cli/commands/model.py``:
  setup_project_path() → setup_sqlite_compat() → load_env(local_mode=True)

Then calls ``maybe_register_memory_backend`` to wire the active provider for
the current process (the registry routers delegate to it).  No full CLI
container is needed for these one-shot commands.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import click


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

def _kb_enabled() -> bool:
    from agents.task.constants import AutonomyConfig
    return AutonomyConfig.kb_enabled()


def _require_kb_enabled() -> bool:
    """Return True if KB is enabled; else print a message and exit non-zero.

    Exits with code 2 (not a silent return/exit 0) so a script can distinguish
    "KB disabled, did nothing" from "succeeded".
    """
    if not _kb_enabled():
        from core.remedy import flag_remedy
        click.echo(click.style(f"KB disabled — {flag_remedy('KB_ENABLED')}", dim=True))
        raise SystemExit(2)
    return True


# ---------------------------------------------------------------------------
# Bootstrap helper: wire the memory provider once per process
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Sync setup preamble (project path + sqlite compat + env load)."""
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    from core.bootstrap import load_env
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    # Set local mode so POLYROB_LOCAL defaults kick in (KB_ENABLED defaulting ON).
    os.environ.setdefault("POLYROB_LOCAL", "1")


async def _ensure_memory_backend() -> None:
    """Wire the active memory provider so the registry routers resolve it.

    Builds the CLI container exactly as ``cli/commands/run.py`` does — this runs
    Task-2's ``maybe_register_cli_embedder``, which lazily registers the
    "embedding_model" service when KB / local_vector / local-mode applies. We then
    register the memory backend WITH that embedder so ``MEMORY_BACKEND=local_vector``
    gets real hybrid vector recall; without the embedder it falls back to FTS-only.
    Fail-open: on any failure we still register an FTS-only backend so search works.
    """
    from modules.memory.backend_factory import maybe_register_memory_backend

    embedder = None
    data_dir = None
    import logging as _logging
    try:
        from core.bootstrap import build_cli_container
        # Suppress the container/LLM INFO boot logs (DependencyContainer registrations,
        # LLM init) so a one-shot `kb` command doesn't spew a wall of logs; restore after.
        _logging.disable(_logging.CRITICAL)
        try:
            container = await build_cli_container(log_level="ERROR")
        finally:
            _logging.disable(_logging.NOTSET)
        if container.has_service("embedding_model"):
            embedder = container.get_service("embedding_model")
        # Reuse the container's project-scoped data_dir (.polyrob/) so the CLI KB lives
        # in the same memory.db the agent uses.
        data_dir = getattr(getattr(container, "config", None), "data_dir", None)
    except Exception as e:
        click.echo(click.style(f"note: container build skipped (FTS-only): {e}", dim=True))

    if not data_dir:
        # FTS-only fallback path: resolve the SAME data home the agent uses —
        # _resolve_cli_data_home honors POLYROB_DATA_DIR (the headless case), so a
        # container-build failure never silently points the KB at the wrong dir.
        try:
            from core.bootstrap import _resolve_cli_data_home
            data_dir = str(_resolve_cli_data_home()[0])
        except Exception:
            from core.runtime_paths import resolve_data_home
            data_dir = str(resolve_data_home())

    try:
        # vector recall needs the embedder; without it this is FTS-only.
        maybe_register_memory_backend(data_dir=data_dir, embedding_model=embedder)
    except Exception as e:
        click.echo(click.style(f"note: memory backend registration skipped: {e}", dim=True))


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------

@click.group("kb")
def kb():
    """Manage the local knowledge base (ingest, search, list, remove)."""
    pass


# ---------------------------------------------------------------------------
# kb export — the canonical vault-export verb (027 WP6 / 030 D10: `kb` and
# `knowledge` were two top-level nouns for one concept; `knowledge` is now a
# hidden deprecated alias and this is the front door — one implementation,
# `run_export` in knowledge.py).
# ---------------------------------------------------------------------------

@kb.command("export")
@click.option("--out", "out_dir", default="./knowledge-vault", show_default=True,
              help="Vault output directory (opened directly in Obsidian).")
@click.option("--since", "since", default=None,
              help="Only episodes newer than this (8h / 2d / ISO date).")
@click.option("--user", "user", default="local", show_default=True,
              help="Tenant to export.")
def kb_export(out_dir: str, since, user: str):
    """Write notes/episodes/skills/identity/goals as a markdown vault."""
    from cli.commands.knowledge import run_export
    run_export(out_dir, since, user)


# ---------------------------------------------------------------------------
# kb add
# ---------------------------------------------------------------------------

@kb.command("add")
@click.argument("path")
@click.option("--collection", default="default", show_default=True, help="KB collection name.")
@click.option("--recursive/--no-recursive", default=True, show_default=True,
              help="Recurse into subdirectories.")
@click.option("--glob", "globs", multiple=True, metavar="PATTERN",
              help="Glob pattern(s) to restrict which files are included (repeatable).")
def kb_add(path: str, collection: str, recursive: bool, globs: tuple) -> None:
    """Ingest a file or directory into the knowledge base."""
    _bootstrap()
    if not _require_kb_enabled():
        return
    asyncio.run(_kb_add(path, collection, recursive, list(globs) or None))


async def _kb_add(path: str, collection: str, recursive: bool, globs) -> None:
    from tools.knowledge_ingest import kb_ingest

    await _ensure_memory_backend()
    session_id = f"cli-kb-{uuid.uuid4().hex[:8]}"
    user_id = "local"

    click.echo(click.style(f"ingesting {path!r} → collection={collection!r} …", dim=True))

    try:
        result = await kb_ingest(
            path,
            collection=collection,
            recursive=recursive,
            globs=globs,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        click.echo(click.style(f"[kb] error: {e}", fg="red"))
        raise SystemExit(1)

    if "error" in result:
        click.echo(click.style(f"[kb] {result['error']}", fg="red"))
        raise SystemExit(1)

    ingested = result.get("ingested", 0)
    unchanged = result.get("unchanged", 0)
    n_chunks = result.get("n_chunks", 0)
    skipped_secret = result.get("skipped_secret", 0)
    skipped_binary = result.get("skipped_binary", 0)
    skipped_office = result.get("skipped_office", 0)

    click.echo(
        click.style("ingested: ", fg="green") + str(ingested) + " file(s), "
        + click.style("chunks: ", fg="green") + str(n_chunks)
    )
    click.echo(click.style("unchanged: ", dim=True) + str(unchanged))
    if skipped_secret or skipped_binary or skipped_office:
        parts = []
        if skipped_secret:
            parts.append(f"secrets={skipped_secret}")
        if skipped_binary:
            parts.append(f"binary={skipped_binary}")
        if skipped_office:
            parts.append(f"office={skipped_office}")
        click.echo(click.style("skipped: ", dim=True) + ", ".join(parts))


# ---------------------------------------------------------------------------
# kb list
# ---------------------------------------------------------------------------

@kb.command("list")
@click.option("--collection", default=None, help="Filter by collection (omit for all).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def kb_list(collection, as_json: bool) -> None:
    """List ingested sources in the knowledge base."""
    _bootstrap()
    if not _require_kb_enabled():
        return
    asyncio.run(_kb_list(collection, as_json))


async def _kb_list(collection, as_json: bool = False) -> None:
    import json

    from modules.memory.registry import kb_list_sources

    await _ensure_memory_backend()
    try:
        sources = await kb_list_sources(user_id="local", collection=collection)
    except Exception as e:
        click.echo(click.style(f"[kb] error: {e}", fg="red"))
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(sources, indent=2, default=str))
        return

    if not sources:
        click.echo(click.style("No sources in KB.", dim=True))
        return

    click.echo(click.style(f"KB sources ({len(sources)}):", fg="cyan"))
    for src in sources:
        click.echo(f"  {src}")


# ---------------------------------------------------------------------------
# kb remove
# ---------------------------------------------------------------------------

@kb.command("remove")
@click.option("--collection", default="default", show_default=True, help="KB collection name.")
@click.option("--source", default=None, help="Source path to remove (omit to clear entire collection).")
def kb_remove(collection: str, source) -> None:
    """Remove a source (or entire collection) from the knowledge base."""
    _bootstrap()
    if not _require_kb_enabled():
        return
    asyncio.run(_kb_remove(collection, source))


async def _kb_remove(collection: str, source) -> None:
    from modules.memory.registry import kb_remove as _registry_remove

    await _ensure_memory_backend()
    try:
        removed = await _registry_remove(user_id="local", collection=collection, source=source)
    except Exception as e:
        click.echo(click.style(f"[kb] error: {e}", fg="red"))
        raise SystemExit(1)

    if source:
        click.echo(f"Removed {removed} chunk(s) from {collection!r} / {source!r}.")
    else:
        click.echo(f"Removed {removed} chunk(s) from collection {collection!r}.")


# ---------------------------------------------------------------------------
# kb search
# ---------------------------------------------------------------------------

@kb.command("search")
@click.argument("query")
@click.option("--collection", default="default", show_default=True, help="KB collection to search.")
@click.option("--limit", default=8, show_default=True, type=int, help="Max results.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def kb_search(query: str, collection: str, limit: int, as_json: bool) -> None:
    """Search the knowledge base for relevant content."""
    _bootstrap()
    if not _require_kb_enabled():
        return
    asyncio.run(_kb_search(query, collection, limit, as_json))


async def _kb_search(query: str, collection: str, limit: int,
                     as_json: bool = False) -> None:
    import json

    from modules.memory.registry import kb_search as _registry_search

    await _ensure_memory_backend()
    try:
        result = await _registry_search(
            query, user_id="local", collection=collection, limit=limit
        )
    except Exception as e:
        click.echo(click.style(f"[kb] error: {e}", fg="red"))
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(
            {"query": query, "collection": collection, "result": result},
            indent=2, default=str))
        return

    if not result:
        click.echo(click.style("No results found.", dim=True))
        return

    click.echo(click.style(f"KB results for {query!r}:", fg="cyan"))
    click.echo(result)


# ---------------------------------------------------------------------------
# kb reindex (WS-K3, 2026-09-22)
# ---------------------------------------------------------------------------

@kb.command("reindex")
@click.option("--user", "user_id", default=None,
              help="Owner tenant (default: this box's owner principal).")
@click.option("--limit", default=0, show_default=True,
              help="Stop after N documents (0 = every one).")
@click.option("--dry-run", is_flag=True,
              help="Name what would be indexed and change nothing.")
def kb_reindex(user_id, limit: int, dry_run: bool) -> None:
    """Index the documents this agent has already written.

    The artifact registry holds one row per produced file; this walks it and
    ingests the ones that are knowledge rather than run logs. Measured on prod
    2026-09-22: 76 indexed sources against 473 registered documents — the
    backfill for everything written before the auto-ingest existed.
    """
    _bootstrap()
    if not _require_kb_enabled():
        return
    from cli._admin_home import admin_data_dir
    from core.admin_data_home import admin_owner_principal

    data_dir = admin_data_dir(write=not dry_run)
    uid = str(user_id or admin_owner_principal())
    asyncio.run(_kb_reindex(uid, data_dir, int(limit or 0), dry_run))


async def _kb_reindex(user_id: str, data_dir: str, limit: int, dry_run: bool) -> None:
    from tools.kb_autoingest import ingest_artifact, should_ingest

    await _ensure_memory_backend()
    db = os.path.join(data_dir, "artifacts.db")
    if not os.path.exists(db):
        click.echo(click.style(f"no artifact registry at {db} — nothing written yet",
                               dim=True))
        raise SystemExit(2)

    from core.sqlite_util import execute_retry
    # ⚠️ The row's OWN session_id rides along: `kb_ingest` confines a path to the
    # session's workspace, and an empty session id resolves to a workspace that
    # does not exist (and, off project-root mode, would be CREATED by the
    # resolver). A backfill must not invent a session directory.
    rows = execute_retry(
        db, "SELECT path, session_id FROM artifacts WHERE user_id = ? "
            "ORDER BY rowid DESC", (user_id,), fetch="all") or []
    entries = [(str(dict(r).get("path") or ""), str(dict(r).get("session_id") or ""))
               for r in rows]

    picked, skipped, missing = [], 0, 0
    for path, session_id in entries:
        if not path:
            continue
        if not os.path.exists(path):
            missing += 1
            continue
        if not should_ingest(path):
            skipped += 1
            continue
        picked.append((path, session_id))
        if limit and len(picked) >= limit:
            break

    click.echo(click.style(
        f"{len(entries)} registered · {len(picked)} to index · {skipped} not "
        f"knowledge (records, images, other binaries) · {missing} gone from disk",
        fg="cyan"))
    if dry_run:
        for path, _sid in picked[:40]:
            click.echo(f"  would index {path}")
        if len(picked) > 40:
            click.echo(f"  … +{len(picked) - 40} more")
        return

    done = unchanged = failed = 0
    for path, session_id in picked:
        result = await ingest_artifact(user_id, path, session_id=session_id,
                                       force=True)
        if result is None:
            failed += 1
            continue
        if result.get("error"):
            failed += 1
            click.echo(click.style(f"  declined {path}: {result['error']}", dim=True))
            continue
        if result.get("unchanged"):
            unchanged += 1
        else:
            done += result.get("ingested") or 0
    click.echo(click.style(
        f"indexed {done} · unchanged {unchanged} · failed {failed}", fg="green"))
